"""
File: mock_esb/server.py
Purpose: Mock ESB on its own port: the 7 brief endpoints with ESB envelope and
         status codes, simulated latency, idempotency store, fault injection and
         config-driven Paket Darurat eligibility rules. Dummy data only.
D2 component: Mock ESB (§2.2, contract §1.5 / §2.5 / §2.6)
Depends on: flask, core/config.py, data/esb_customers.json
"""

import copy
import hashlib
import json
import random
import threading
import time
import uuid
from datetime import date, timedelta

from flask import Blueprint, Flask, jsonify, request

from core.config import ROOT_DIR, rule

SEED_PATH = ROOT_DIR / "data" / "esb_customers.json"

STATE = {}                      # live, mutable copy of the seed data
STATE_LOCK = threading.Lock()   # mock runs threaded: protect balance + idempotency store

esb = Blueprint("esb", __name__, url_prefix="/esb")


def reset_state() -> None:
    """
    Reload the seed data and clear the idempotency store, ledger and used faults.

    Why: repeatable demos and tests (POST /api/reset calls this through /esb/_admin/reset).
    Args:
        None
    Returns:
        None
    Raises / Errors:
        FileNotFoundError: if the seed file is missing.
    Side effects:
        Replaces STATE in memory.
    """
    with open(SEED_PATH, encoding="utf-8") as seed_file:
        seed = json.load(seed_file)
    with STATE_LOCK:
        STATE.clear()
        STATE["customers"] = copy.deepcopy(seed["customers"])
        STATE["cells"] = copy.deepcopy(seed["cells"])
        STATE["idempotency"] = {}   # key -> {"payload_hash", "http_status", "body"}; TTL 24 h documented (DEF-03)
        STATE["ledger"] = []        # every real charge, used to PROVE "no double purchase"
        STATE["used_faults"] = set()


def envelope(http_status: int, status_code: str, status_desc: str, transaction_id, **data):
    """
    Build an ESB-style response: {"transaction": {...}, ...data}.

    Why: §2.5 response envelope convention.
    Args:
        http_status (int): HTTP status to return.
        status_code (str): ESB status code, e.g. "00000", "30101".
        status_desc (str): human-readable description.
        transaction_id (str | None): echoed from the request.
        **data: response payload sections (profile, quota, purchase, ...).
    Returns:
        tuple: (flask Response, http_status)
    Raises / Errors:
        None
    Side effects:
        None
    """
    body = {
        "transaction": {
            "transaction_id": transaction_id,
            "channel": rule("pilot", "channel"),
            "status_code": status_code,
            "status_desc": status_desc,
        },
        **data,
    }
    return jsonify(body), http_status


def request_transaction_id():
    """
    Read transaction_id from the POST envelope or the optional GET query parameter.

    Why: §2.5 — every ESB response echoes the caller's transaction_id.
    Args:
        None (reads the current Flask request)
    Returns:
        str | None: the transaction_id, if sent.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        return (body.get("transaction") or {}).get("transaction_id")
    return request.args.get("transaction_id")


@esb.before_request
def check_api_key():
    """
    Reject calls without the API key header (SIMPLIFIED auth: presence only).

    Why: §2.5 header convention; full x-signature verification is DEF-04.
    Args:
        None
    Returns:
        None to continue, or an error envelope.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if request.path.startswith("/esb/_admin"):
        return None  # mock-only admin endpoints
    # TODO[DEFERRED-DEF-04]: Verify x-signature = md5(api_key + secret + timestamp)
    # Why deferred: demo needs only header presence; signature check adds no visible value
    # Call here when implemented: verify_signature(headers: dict) -> bool
    # D2 reference: §2.5 request headers
    if not request.headers.get(rule("esb", "api_key_header")):
        return envelope(400, "20001", "Missing api_key header", request_transaction_id())
    return None


def find_customer(msisdn: str):
    """
    Look up a customer record by MSISDN.

    Why: all subscriber endpoints share this lookup and its 20002 error.
    Args:
        msisdn (str): 62xxxxxxxxxx
    Returns:
        dict | None: the live customer record, or None if unknown.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return STATE["customers"].get(msisdn)


@esb.get("/subscriber/<msisdn>/profile")
def get_profile(msisdn):
    """
    GET /subscriber/{msisdn}/profile — profile, segment, consent (contains PII).

    Why: brief endpoint 1; consent check at conversation start (UC1).
    Args:
        msisdn (str): path parameter.
    Returns:
        Envelope with profile{name (tokenized), segment, region, consent}.
    Raises / Errors:
        400/20002 if the MSISDN is unknown.
    Side effects:
        None
    """
    customer = find_customer(msisdn)
    if customer is None:
        return envelope(400, "20002", "Invalid MSISDN", request_transaction_id())
    profile = {key: value for key, value in customer["profile"].items() if key != "display_name"}
    return envelope(200, "00000", "Success", request_transaction_id(), profile=profile)


@esb.get("/subscriber/<msisdn>/quota")
def get_quota(msisdn):
    """
    GET /subscriber/{msisdn}/quota — remaining quota, FUP status, pulsa.

    Why: brief endpoint 2, with simulated 200–800 ms latency.
    Args:
        msisdn (str): path parameter.
    Returns:
        Envelope with quota{items}, fup{status, speed_kbps}, balance{pulsa_idr}.
    Raises / Errors:
        400/20002 if the MSISDN is unknown.
    Side effects:
        Sleeps for a random latency inside the configured range.
    """
    low_ms, high_ms = rule("mock_esb", "quota_latency_ms")
    time.sleep(random.uniform(low_ms, high_ms) / 1000)  # brief: response time 200–800 ms
    customer = find_customer(msisdn)
    if customer is None:
        return envelope(400, "20002", "Invalid MSISDN", request_transaction_id())
    return envelope(200, "00000", "Success", request_transaction_id(),
                    quota=customer["quota"], fup=customer["fup"], balance=customer["balance"])


@esb.get("/offers")
def get_offers():
    """
    GET /offers?msisdn= — eligible packages (BID) with price and validity.

    Why: brief endpoint 3.
    Args:
        msisdn (str): query parameter.
    Returns:
        Envelope with offers[...].
    Raises / Errors:
        400/20002 if the MSISDN is unknown.
    Side effects:
        None
    """
    customer = find_customer(request.args.get("msisdn", ""))
    if customer is None:
        return envelope(400, "20002", "Invalid MSISDN", request_transaction_id())
    return envelope(200, "00000", "Success", request_transaction_id(), offers=customer["offers"])


def evaluate_loan(customer: dict, amount_idr: int) -> dict:
    """
    Apply the configured Paket Darurat eligibility rules to one customer and amount.

    Why: UC4; rules live in config.toml [mock_esb.loan_rules] so they can change at H+2.
    Args:
        customer (dict): live customer record (uses loan_profile).
        amount_idr (int): requested loan amount = package price.
    Returns:
        dict: {eligible, limit_idr, fee_idr, repayment, reasons[]}
    Raises / Errors:
        None
    Side effects:
        None
    """
    rules = rule("mock_esb", "loan_rules")
    loan_profile = customer["loan_profile"]
    reasons = []
    if loan_profile["tenure_days"] < rules["min_tenure_days"]:
        reasons.append("tenure_too_short")
    if loan_profile["avg_monthly_topup_idr"] < rules["min_avg_monthly_topup_idr"]:
        reasons.append("topup_history_too_low")
    if rules["block_if_outstanding_loan"] and loan_profile["has_outstanding_loan"]:
        reasons.append("outstanding_loan")
    if amount_idr > rules["limit_idr"]:
        reasons.append("amount_above_limit")
    if rules["fee_type"] == "percent":
        fee_idr = round(amount_idr * rules["fee_percent"] / 100)
    else:
        fee_idr = rules["fee_flat_idr"]
    return {"eligible": not reasons, "limit_idr": rules["limit_idr"], "fee_idr": fee_idr,
            "repayment": rules["repayment_text"], "reasons": reasons}


@esb.post("/loan/eligibility")
def loan_eligibility():
    """
    POST /loan/eligibility — check Paket Darurat; returns limit and fee.

    Why: brief endpoint 5 (UC4).
    Args:
        JSON body: envelope + service.service_id (msisdn) + loan{offer_id, amount_idr}.
    Returns:
        200/00000 with loan{eligible: true, ...}, or 400/30301 with loan{eligible: false, ...}.
    Raises / Errors:
        400/20002 if the MSISDN is unknown.
    Side effects:
        None
    """
    body = request.get_json(silent=True) or {}
    customer = find_customer((body.get("service") or {}).get("service_id", ""))
    if customer is None:
        return envelope(400, "20002", "Invalid MSISDN", request_transaction_id())
    loan_request = body.get("loan") or {}
    result = evaluate_loan(customer, int(loan_request.get("amount_idr", 0)))
    if not result["eligible"]:
        return envelope(400, "30301", "Not eligible for Paket Darurat", request_transaction_id(), loan=result)
    return envelope(200, "00000", "Success", request_transaction_id(), loan=result)


@esb.get("/network/status")
def network_status():
    """
    GET /network/status?cell= — network status for a cell: normal or degraded.

    Why: brief endpoint 6 (UC1 / UC5).
    Args:
        cell (str): query parameter.
    Returns:
        Envelope with network{cell_id, status, incident_ref, updated_at}.
    Raises / Errors:
        400/30401 for an unknown cell.
    Side effects:
        None
    """
    cell_id = request.args.get("cell", "")
    cell = STATE["cells"].get(cell_id)
    if cell is None:
        return envelope(400, "30401", "Unknown cell", request_transaction_id())
    network = {"cell_id": cell_id, "status": cell["status"], "incident_ref": cell["incident_ref"],
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    return envelope(200, "00000", "Success", request_transaction_id(), network=network)


def payload_hash(payload: dict) -> str:
    """
    Hash a request payload so a retried request can be compared to the original.

    Why: §2.7 — same key + same payload = replay; same key + different payload = 409.
    Args:
        payload (dict): the business part of the request.
    Returns:
        str: sha256 hex digest of the canonical JSON.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def check_idempotency(key: str, current_hash: str):
    """
    Return a stored response for a repeated Idempotency-Key, or None if the key is new.

    Why: §2.7 mock idempotency — never charge twice for one key.
    Args:
        key (str): Idempotency-Key header value.
        current_hash (str): payload hash of this request.
    Returns:
        tuple | None: (body, http_status) to send back, or None to process normally.
    Raises / Errors:
        None (a conflict is returned as a 409/30109 body).
    Side effects:
        None
    """
    stored = STATE["idempotency"].get(key)
    if stored is None:
        return None
    if stored["payload_hash"] != current_hash:
        conflict = {"transaction": {"transaction_id": None, "channel": rule("pilot", "channel"),
                                    "status_code": "30109", "status_desc": "Idempotency key conflict"}}
        return conflict, 409
    replay_body = copy.deepcopy(stored["body"])
    for section in ("purchase", "ticket"):
        if section in replay_body:
            replay_body[section]["idempotent_replay"] = True   # original result, no new charge
    # TODO[DEFERRED-DEF-12]: "key in progress" -> 409, client waits and retries with the same key
    # Why deferred: the mock stores the result BEFORE any simulated delay, so this race never occurs
    # Call here when implemented: is_key_in_progress(key: str) -> bool
    # D2 reference: §2.7 mock server idempotency
    return replay_body, stored["http_status"]


def apply_purchase(msisdn: str, customer: dict, offer: dict, payment_method: str):
    """
    Charge the customer and activate the package (PULSA or PAKET_DARURAT).

    Why: UC3 / UC4 business effect; every charge is written to the ledger.
    Args:
        msisdn (str): customer MSISDN.
        customer (dict): live customer record.
        offer (dict): the eligible offer being bought.
        payment_method (str): "PULSA" or "PAKET_DARURAT".
    Returns:
        tuple: (http_status, status_code, status_desc, purchase_section | None)
    Raises / Errors:
        None (business errors are returned as 30101 / 30301).
    Side effects:
        Changes pulsa / loan state and quota; appends to STATE["ledger"].
    """
    price = offer["price_idr"]
    if payment_method == "PAKET_DARURAT":
        loan = evaluate_loan(customer, price)
        if not loan["eligible"]:
            return 400, "30301", "Not eligible for Paket Darurat", None
        customer["loan_profile"]["has_outstanding_loan"] = True  # debt repaid at next top-up
        charged = 0
    else:
        if price > customer["balance"]["pulsa_idr"]:
            return 400, "30101", "Insufficient pulsa", None
        customer["balance"]["pulsa_idr"] -= price
        charged = price
    expiry = (date.today() + timedelta(days=offer["validity_days"])).isoformat()
    customer["quota"]["items"].append({"type": "internet", "remaining_mb": offer["quota_mb"],
                                       "total_mb": offer["quota_mb"], "expiry": expiry})
    if offer["category"] == "booster":
        customer["fup"] = {"status": "NORMAL", "speed_kbps": None}  # booster lifts the FUP throttle
    order_id = "ORD-" + uuid.uuid4().hex[:10].upper()
    STATE["ledger"].append({"order_id": order_id, "msisdn": msisdn, "offer_id": offer["offer_id"],
                            "payment_method": payment_method, "charged_idr": charged})
    section = {"order_id": order_id, "status": "SUCCESS", "charged_idr": charged,
               "payment_method": payment_method, "idempotent_replay": False}
    return 200, "00000", "Success", section


def validate_purchase_request(body: dict, key: str):
    """
    Check the mandatory parts of a purchase request.

    Why: §2.6 — 20002 for a missing Idempotency-Key or confirmation; 30102 for a non-eligible offer.
    Args:
        body (dict): request JSON.
        key (str): Idempotency-Key header (may be empty).
    Returns:
        tuple: (error_tuple | None, customer | None, offer | None, purchase_request dict)
    Raises / Errors:
        None
    Side effects:
        None
    """
    purchase_request = body.get("purchase") or {}
    msisdn = (body.get("service") or {}).get("service_id", "")
    customer = find_customer(msisdn)
    if not key or not purchase_request.get("confirmation_id") or purchase_request.get("is_confirmation") != "1":
        return (400, "20002", "Missing Idempotency-Key or confirmation"), None, None, purchase_request
    if customer is None:
        return (400, "20002", "Invalid MSISDN"), None, None, purchase_request
    offer = next((o for o in customer["offers"] if o["offer_id"] == purchase_request.get("offer_id")), None)
    if offer is None:
        return (400, "30102", "Offer not eligible"), customer, None, purchase_request
    return None, customer, offer, purchase_request


@esb.post("/purchase")
def purchase():
    """
    POST /purchase — buy a package. Requires Idempotency-Key; can time out.

    Why: brief endpoint 4; demonstrates idempotent replay after a timeout (P5).
    Args:
        Header Idempotency-Key; JSON envelope + purchase{offer_id, payment_method, confirmation_id, is_confirmation}.
    Returns:
        Envelope with purchase{order_id, status, charged_idr, idempotent_replay}.
    Raises / Errors:
        400/20002, 400/30101, 400/30102, 400/30301, 409/30109.
    Side effects:
        Charges once per key; fault "purchase_timeout_once" stores the result FIRST and then
        sleeps past the client timeout (charge succeeded, response lost).
    """
    body = request.get_json(silent=True) or {}
    key = request.headers.get("Idempotency-Key", "")
    tx_id = request_transaction_id()
    with STATE_LOCK:
        error, customer, offer, purchase_request = validate_purchase_request(body, key)
        msisdn = (body.get("service") or {}).get("service_id", "")
        current_hash = payload_hash({"msisdn": msisdn, **purchase_request})
        replay = check_idempotency(key, current_hash) if key else None
        if replay is not None:
            return jsonify(replay[0]), replay[1]
        if error is not None:
            return envelope(error[0], error[1], error[2], tx_id)
        http_status, code, desc, section = apply_purchase(msisdn, customer, offer,
                                                          purchase_request.get("payment_method", "PULSA"))
        response_body = {"transaction": {"transaction_id": tx_id, "channel": rule("pilot", "channel"),
                                         "status_code": code, "status_desc": desc}}
        if section is not None:
            response_body["purchase"] = section
        STATE["idempotency"][key] = {"payload_hash": current_hash, "http_status": http_status, "body": response_body}
        inject_timeout = "purchase_timeout_once" in customer["faults"] and (msisdn, "purchase") not in STATE["used_faults"]
        if inject_timeout:
            STATE["used_faults"].add((msisdn, "purchase"))
    if inject_timeout:
        time.sleep(rule("mock_esb", "purchase_timeout_delay_s"))  # longer than the client timeout
    return jsonify(response_body), http_status


@esb.post("/ticket")
def create_ticket():
    """
    POST /ticket — create a network trouble ticket. Requires Idempotency-Key.

    Why: brief endpoint 7 (UC5).
    Args:
        Header Idempotency-Key; JSON envelope + ticket{cell_id, category, symptom, evidence}.
    Returns:
        Envelope with ticket{ticket_no, status OPEN, idempotent_replay}.
    Raises / Errors:
        400/20002 missing key; 502/30501 when fault "ticket_fail" is set.
    Side effects:
        Stores the ticket under its Idempotency-Key.
    """
    body = request.get_json(silent=True) or {}
    key = request.headers.get("Idempotency-Key", "")
    tx_id = request_transaction_id()
    if not key:
        return envelope(400, "20002", "Missing Idempotency-Key", tx_id)
    with STATE_LOCK:
        customer = find_customer((body.get("service") or {}).get("service_id", "")) or {"faults": []}
        if "ticket_fail" in customer["faults"]:
            return envelope(502, "30501", "Ticket creation failed", tx_id)
        replay = check_idempotency(key, payload_hash(body.get("ticket") or {}))
        if replay is not None:
            return jsonify(replay[0]), replay[1]
        ticket = {"ticket_no": "TT-" + uuid.uuid4().hex[:8].upper(), "status": "OPEN", "idempotent_replay": False}
        response_body = {"transaction": {"transaction_id": tx_id, "channel": rule("pilot", "channel"),
                                         "status_code": "00000", "status_desc": "Success"}, "ticket": ticket}
        STATE["idempotency"][key] = {"payload_hash": payload_hash(body.get("ticket") or {}),
                                     "http_status": 200, "body": response_body}
    return jsonify(response_body), 200


@esb.post("/_admin/reset")
def admin_reset():
    """
    Mock-only: reset all mock data.

    Why: repeatable demos and tests. Not part of the brief's contract.
    Args:
        None
    Returns:
        JSON {"reset": true}
    Raises / Errors:
        None
    Side effects:
        Calls reset_state().
    """
    reset_state()
    return jsonify({"reset": True})


@esb.get("/_admin/ledger")
def admin_ledger():
    """
    Mock-only: list every real charge, to prove a single charge after a timeout.

    Why: §1.4 "never purchase twice" must be verifiable in tests and the demo.
    Args:
        msisdn (str, optional): query filter.
    Returns:
        JSON {"charges": [...]}
    Raises / Errors:
        None
    Side effects:
        None
    """
    msisdn = request.args.get("msisdn")
    charges = [c for c in STATE["ledger"] if msisdn is None or c["msisdn"] == msisdn]
    return jsonify({"charges": charges})


def create_esb_app() -> Flask:
    """
    Create the mock ESB Flask app with fresh seed data.

    Why: runs on its own port (ESB_PORT) so it behaves like an external system.
    Args:
        None
    Returns:
        Flask: the mock ESB application.
    Raises / Errors:
        FileNotFoundError: if the seed file is missing.
    Side effects:
        Loads seed data into STATE.
    """
    reset_state()
    app = Flask("mock_esb")
    app.register_blueprint(esb)
    return app
