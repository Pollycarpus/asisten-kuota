"""
File: core/tools.py
Purpose: Agent tool JSON schemas and their executors. The LLM fills only business
         choices (offer_id, payment_method, symptom, reason); the system fills
         MSISDN, cell, prices, keys and IDs. Gated tools only create a confirmation card.
D2 component: Agent tools (§2.3 UC1–UC5) + guardrail calls
Depends on: core/esb_adapter.py, core/diagnosis.py, core/guardrails.py, core/transactions.py, core/trace.py, core/config.py
"""

import json

from core import guardrails, trace, transactions
from core.config import rule
from core.diagnosis import diagnose, rank_offers
from core.esb_adapter import call_esb


def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    """
    Build one tool schema in Chat Completions function-calling format.

    Why: keeps TOOL_SCHEMAS short and uniform.
    Args:
        name (str), description (str), properties (dict), required (list)
    Returns:
        dict: tool schema.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOL_SCHEMAS = [
    _schema("get_quota", "Read the customer's remaining internet quota, FUP status and pulsa balance.", {}, []),
    _schema("get_network_status", "Read the network status in the customer's area. Returns the diagnosis once quota is known.", {}, []),
    _schema("get_offers", "Get the Top-3 packages that fit the diagnosis (empty when the network is degraded).", {}, []),
    _schema("check_paket_darurat", "Check if Paket Darurat (emergency loan) can pay for one Top-3 package when pulsa is not enough.",
            {"offer_id": {"type": "string", "description": "offer_id from get_offers"}}, ["offer_id"]),
    _schema("purchase", "Request purchase of one Top-3 package. This ONLY shows a confirmation button to the customer; "
            "the system buys after the customer taps it.",
            {"offer_id": {"type": "string"}, "payment_method": {"type": "string", "enum": ["PULSA", "PAKET_DARURAT"]}},
            ["offer_id", "payment_method"]),
    _schema("create_ticket", "Request a network trouble ticket (only for a real network problem). Shows a confirmation button.",
            {"symptom": {"type": "string", "description": "short description of the problem, no personal data"}}, ["symptom"]),
    _schema("request_handoff", "Hand the conversation to a human agent.",
            {"reason": {"type": "string"}}, ["reason"]),
]


def tool_get_quota(session, args: dict) -> tuple:
    """
    Read quota/FUP/pulsa from the ESB and keep only the numbers the LLM needs.

    Why: UC1; §2.8 data minimization (no MSISDN in the result).
    Args:
        session (Session): conversation (MSISDN comes from here, never from the LLM).
        args (dict): none.
    Returns:
        tuple: (result_for_llm dict, card dict | None)
    Raises / Errors:
        None (ESB failures become {"error": ...}).
    Side effects:
        ESB GET /quota; may compute the diagnosis.
    """
    result = call_esb(session, "quota", "GET", f"/subscriber/{session.msisdn}/quota")
    if not result["ok"]:
        return {"error": "quota_unavailable"}, None
    data = result["data"]
    internet = [item for item in data["quota"]["items"] if item["type"] == "internet"]
    session.quota = {
        "internet_remaining_mb": sum(item["remaining_mb"] for item in internet),
        "internet_total_mb": sum(item["total_mb"] for item in internet),
        "fup_status": data["fup"]["status"],
        "fup_speed_kbps": data["fup"]["speed_kbps"],
        "pulsa_idr": data["balance"]["pulsa_idr"],
    }
    return {**session.quota, **diagnosis_section(session)}, diagnosis_card(session)


def tool_get_network_status(session, args: dict) -> tuple:
    """
    Read the network status for the customer's cell; the LLM only sees "area Anda".

    Why: UC1 / UC5; §2.8 — precise cell location never goes to the LLM.
    Args:
        session (Session): conversation (cell_id from the session).
        args (dict): none.
    Returns:
        tuple: (result_for_llm, card | None)
    Raises / Errors:
        None
    Side effects:
        ESB GET /network/status; may compute the diagnosis.
    """
    result = call_esb(session, "network", "GET", "/network/status", params={"cell": session.cell_id})
    if not result["ok"]:
        return {"error": "network_status_unavailable"}, None
    network = result["data"]["network"]
    session.network = {"status": network["status"], "incident_ref": network["incident_ref"]}
    return {"area": "area Anda", **session.network, **diagnosis_section(session)}, diagnosis_card(session)


def diagnosis_section(session) -> dict:
    """
    Run the rules engine once both quota and network are known.

    Why: AD-3 — the diagnosis is deterministic; the LLM only explains it.
    Args:
        session (Session): conversation.
    Returns:
        dict: {"diagnosis": {...}} or {} if data is still missing.
    Raises / Errors:
        None
    Side effects:
        Sets session.diagnosis; trace event.
    """
    if session.quota is None or session.network is None:
        return {}
    if session.diagnosis is None:
        session.diagnosis = diagnose(session.quota, session.network)
        trace.log(session, "diagnosis", f"Rules engine: {session.diagnosis['status']} ({session.diagnosis['reason']})")
    return {"diagnosis": session.diagnosis}


def diagnosis_card(session) -> dict | None:
    """
    Build the diagnosis card for the UI (once).

    Why: Chat UI diagnosis card (§2.2, SIMPLIFIED).
    Args:
        session (Session): conversation.
    Returns:
        dict | None: card, or None if no diagnosis yet.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if session.diagnosis is None:
        return None
    return {"type": "diagnosis", "status": session.diagnosis["status"], "quota": session.quota,
            "network": session.network["status"], "incident_ref": session.network["incident_ref"]}


def tool_get_offers(session, args: dict) -> tuple:
    """
    Fetch eligible offers and return only the ranked Top-N (the whitelist).

    Why: UC2; upsell suppression when the network is degraded.
    Args:
        session (Session): conversation.
        args (dict): none.
    Returns:
        tuple: (result_for_llm, offers card | None)
    Raises / Errors:
        None
    Side effects:
        ESB GET /offers; sets session.top_offers; trace event.
    """
    if session.diagnosis is None:
        return {"error": "call get_quota and get_network_status first"}, None
    if session.diagnosis["network_degraded"]:
        trace.log(session, "guardrail", "Upsell suppressed: network degraded")
        return {"offers": [], "note": "upsell suppressed because the network is degraded"}, None
    result = call_esb(session, "offers", "GET", "/offers", params={"msisdn": session.msisdn})
    if not result["ok"]:
        return {"error": "offers_unavailable"}, None
    session.top_offers = rank_offers(result["data"]["offers"], session.diagnosis, session.quota["pulsa_idr"])
    trace.log(session, "guardrail", f"Whitelist set: {[o['offer_id'] for o in session.top_offers]}")
    return {"offers": session.top_offers, "pulsa_idr": session.quota["pulsa_idr"]}, \
        {"type": "offers", "offers": session.top_offers}


def tool_check_paket_darurat(session, args: dict) -> tuple:
    """
    Apply the Paket Darurat policy, then ask the ESB for eligibility, limit and fee.

    Why: UC4 — policy from config.toml [paket_darurat]; eligibility from the ESB.
    Args:
        session (Session): conversation.
        args (dict): {"offer_id": str}
    Returns:
        tuple: (result_for_llm, None)
    Raises / Errors:
        None
    Side effects:
        ESB POST /loan/eligibility; stores session.loan_checks; trace events.
    """
    offer = guardrails.is_whitelisted(session, args.get("offer_id", ""))
    if offer is None:
        trace.log(session, "guardrail", f"Rejected offer {args.get('offer_id')}: not in Top-3 whitelist")
        return {"error": "offer_not_in_top3"}, None
    allowed, reason = guardrails.paket_darurat_policy(session, offer)
    trace.log(session, "guardrail", f"Paket Darurat policy: {reason}")
    base = {"offer_id": offer["offer_id"], "offer_name": offer["name"], "price_idr": offer["price_idr"],
            "topup_link": rule("paket_darurat", "topup_link")}
    if not allowed:
        result = {**base, "eligible": False, "reason": reason}
    else:
        esb = call_esb(session, "loan", "POST", "/loan/eligibility",
                       payload={"loan": {"offer_id": offer["offer_id"], "amount_idr": offer["price_idr"]}})
        loan = esb["data"].get("loan") or {}
        if not esb["ok"] and esb["status_code"] != "30301":
            return {"error": "loan_service_unavailable"}, None
        result = {**base, "eligible": bool(loan.get("eligible")), "limit_idr": loan.get("limit_idr"),
                  "fee_idr": loan.get("fee_idr"), "repayment": loan.get("repayment"), "reasons": loan.get("reasons", [])}
    session.loan_checks[offer["offer_id"]] = result
    return result, None


def tool_purchase(session, args: dict) -> tuple:
    """
    GATED: validate the request and show a confirmation card. Never buys here.

    Why: AD-4 confirmation gate; whitelist; UC4 trigger when pulsa is insufficient; purchase lock.
    Args:
        session (Session): conversation.
        args (dict): {"offer_id": str, "payment_method": "PULSA" | "PAKET_DARURAT"}
    Returns:
        tuple: (result_for_llm, confirmation card | None)
    Raises / Errors:
        None
    Side effects:
        Creates a pending confirmation; trace events.
    """
    offer = guardrails.is_whitelisted(session, args.get("offer_id", ""))
    if offer is None:
        trace.log(session, "guardrail", f"Rejected purchase of {args.get('offer_id')}: not in Top-3 whitelist")
        return {"error": "offer_not_in_top3"}, None
    if transactions.purchase_locked(session):
        trace.log(session, "guardrail", "Purchase request blocked: lock active")
        return {"error": "purchase_in_progress"}, None
    method = args.get("payment_method", "PULSA")
    fee = 0
    if method == "PAKET_DARURAT":
        loan = session.loan_checks.get(offer["offer_id"])
        if not loan or not loan["eligible"]:
            trace.log(session, "guardrail", "Rejected PAKET_DARURAT: eligibility not confirmed for this offer")
            return {"error": "paket_darurat_not_confirmed_call_check_paket_darurat"}, None
        fee = loan["fee_idr"]
    elif offer["price_idr"] > session.quota["pulsa_idr"]:
        trace.log(session, "guardrail", "Pulsa insufficient for PULSA payment -> suggest check_paket_darurat (UC4)")
        return {"error": "insufficient_pulsa", "next": "call check_paket_darurat"}, None
    card = transactions.create_confirmation(session, "purchase", {
        "offer": offer, "payment_method": method, "price_idr": offer["price_idr"], "fee_idr": fee,
        "total_idr": offer["price_idr"] + fee,
        "repayment": session.loan_checks.get(offer["offer_id"], {}).get("repayment") if fee else None})
    return {"status": "awaiting_customer_confirmation", "note": "customer must tap Konfirmasi; do not say it is bought"}, card


def tool_create_ticket(session, args: dict) -> tuple:
    """
    GATED: show a [Buat laporan] confirmation for a genuine network problem.

    Why: UC5 — tickets only when the diagnosis is NETWORK_DEGRADED (no false network tickets); 1 per conversation.
    Args:
        session (Session): conversation.
        args (dict): {"symptom": str}
    Returns:
        tuple: (result_for_llm, confirmation card | None)
    Raises / Errors:
        None
    Side effects:
        Creates a pending confirmation; trace events.
    """
    if session.ticket:
        return {"status": "ticket_exists", "ticket_no": session.ticket["ticket_no"]}, None
    if not session.diagnosis or session.diagnosis["status"] != "NETWORK_DEGRADED":
        trace.log(session, "guardrail", "Ticket refused: diagnosis is not NETWORK_DEGRADED")
        return {"error": "no_network_problem_detected"}, None
    card = transactions.create_confirmation(session, "ticket", {
        "symptom": str(args.get("symptom", ""))[:200], "incident_ref": session.network["incident_ref"]})
    return {"status": "awaiting_customer_confirmation"}, card


def tool_request_handoff(session, args: dict) -> tuple:
    """
    Hand off to a human at the LLM's request (safe direction: it can only escalate).

    Why: §2.10 — out-of-authority requests.
    Args:
        session (Session): conversation.
        args (dict): {"reason": str}
    Returns:
        tuple: (result_for_llm, handoff card)
    Raises / Errors:
        None
    Side effects:
        Ends AI processing for the session.
    """
    card = guardrails.do_handoff(session, "llm_requested: " + str(args.get("reason", ""))[:80])
    return {"status": "handed_off", "ref_id": card["ref_id"]}, card


EXECUTORS = {
    "get_quota": tool_get_quota,
    "get_network_status": tool_get_network_status,
    "get_offers": tool_get_offers,
    "check_paket_darurat": tool_check_paket_darurat,
    "purchase": tool_purchase,
    "create_ticket": tool_create_ticket,
    "request_handoff": tool_request_handoff,
}


def execute(session, name: str, arguments: dict) -> tuple:
    """
    Run one tool call requested by the LLM and trace it.

    Why: single dispatch point so every tool call is traced (D4 trace requirement).
    Args:
        session (Session): conversation.
        name (str): tool name from the LLM.
        arguments (dict): tool arguments from the LLM.
    Returns:
        tuple: (result_for_llm dict, card dict | None)
    Raises / Errors:
        None (unknown tools return {"error": ...}).
    Side effects:
        Whatever the tool does; "tool_call" and "tool_result" trace events.
    """
    trace.log(session, "tool_call", f"LLM requested tool {name}({json.dumps(arguments, ensure_ascii=False)})")
    executor = EXECUTORS.get(name)
    if executor is None:
        result, card = {"error": f"unknown tool {name}"}, None
    else:
        result, card = executor(session, arguments or {})
    trace.log(session, "tool_result", f"{name} -> {json.dumps(result, ensure_ascii=False)[:180]}")
    return result, card
