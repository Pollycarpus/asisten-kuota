"""
File: core/transactions.py
Purpose: The confirmation gate and the purchase/ticket state machine. A purchase or
         ticket executes ONLY here, after a button tap (POST /api/confirm).
         1 confirmation = 1 transaction_id + 1 Idempotency-Key; retries reuse them.
D2 component: Transaction state store + confirmation gate (§2.2, §2.4, AD-4, AD-5)
Depends on: core/config.py, core/esb_adapter.py, core/guardrails.py, core/trace.py, core/privacy.py
"""

import time
import uuid

from core import guardrails, trace
from core.config import rule
from core.esb_adapter import call_esb, new_transaction_id
from core.privacy import hash_msisdn

LOCKING_STATES = {"SUBMITTING", "UNKNOWN", "PENDING_VERIFICATION"}   # block a second purchase


def set_state(session, transaction: dict, new_state: str, reason: str = "") -> None:
    """
    Move a transaction to a new state and trace the transition.

    Why: §2.4 state machine; §2.7 every state transition is written to the trace.
    Args:
        session (Session): conversation.
        transaction (dict): the transaction record.
        new_state (str): target state.
        reason (str): short explanation.
    Returns:
        None
    Raises / Errors:
        None
    Side effects:
        Updates transaction["state"]; one "state" trace event.
    """
    old_state = transaction["state"]
    transaction["state"] = new_state
    trace.log(session, "state", f"{transaction['kind']} {old_state} -> {new_state}" + (f" ({reason})" if reason else ""),
              transaction_id=transaction.get("transaction_id"))


def purchase_locked(session) -> bool:
    """
    True if a purchase is in flight or unresolved in this conversation.

    Why: §1.4 never purchase twice — lock while SUBMITTING, UNKNOWN or PENDING_VERIFICATION.
    Args:
        session (Session): conversation.
    Returns:
        bool
    Raises / Errors:
        None
    Side effects:
        None
    """
    return any(t["kind"] == "purchase" and t["state"] in LOCKING_STATES for t in session.transactions)


def create_confirmation(session, kind: str, details: dict) -> dict:
    """
    Intercept a gated tool call and create a pending confirmation (no transaction yet).

    Why: AD-4 — purchase/create_ticket run only after a structured confirmation event from the UI.
    Args:
        session (Session): conversation.
        kind (str): "purchase" or "ticket".
        details (dict): purchase: offer, payment_method, fee_idr; ticket: symptom.
    Returns:
        dict: UI card {type: "confirmation", confirmation_id, kind, ...}.
    Raises / Errors:
        None
    Side effects:
        Stores the confirmation in the session; trace events.
    """
    confirmation_id = "CNF-" + uuid.uuid4().hex[:10].upper()
    confirmation = {"confirmation_id": confirmation_id, "kind": kind, "details": details,
                    "expires_at": time.time() + rule("agent", "confirmation_ttl_s"), "used": False}
    session.confirmations[confirmation_id] = confirmation
    trace.log(session, "guardrail", f"Gated tool '{kind}' intercepted -> AWAITING_CONFIRMATION ({confirmation_id})",
              confirmation_id=confirmation_id, expires_in_s=rule("agent", "confirmation_ttl_s"))
    return {"type": "confirmation", "confirmation_id": confirmation_id, "kind": kind, **details}


def consume_confirmation(session, confirmation_id: str) -> tuple:
    """
    Validate a confirmation and mark it used (single use).

    Why: AD-4 — confirmation_id is single-use and expires after 5 minutes.
    Args:
        session (Session): conversation.
        confirmation_id (str): from the button tap.
    Returns:
        tuple: (confirmation dict | None, error_code | None) where error_code is
               "unknown", "already_used" or "expired".
    Raises / Errors:
        None
    Side effects:
        Marks the confirmation used; trace event.
    """
    confirmation = session.confirmations.get(confirmation_id)
    if confirmation is None:
        return None, "unknown"
    if confirmation["used"]:
        trace.log(session, "guardrail", f"Confirmation {confirmation_id} rejected: already used (double tap blocked)")
        return None, "already_used"
    if time.time() > confirmation["expires_at"]:
        confirmation["used"] = True
        trace.log(session, "guardrail", f"Confirmation {confirmation_id} rejected: expired")
        return None, "expired"
    confirmation["used"] = True   # consume once, BEFORE any ESB call
    trace.log(session, "guardrail", f"Confirmation {confirmation_id} accepted (button tap)")
    return confirmation, None


def cancel_confirmation(session, confirmation_id: str) -> bool:
    """
    Cancel a pending confirmation ([Batal]).

    Why: §2.4 AWAITING_CONFIRMATION -> CANCELLED.
    Args:
        session (Session): conversation.
        confirmation_id (str): from the button tap.
    Returns:
        bool: True if it was pending and is now cancelled.
    Raises / Errors:
        None
    Side effects:
        Marks the confirmation used; trace event.
    """
    confirmation = session.confirmations.get(confirmation_id)
    if confirmation is None or confirmation["used"]:
        return False
    confirmation["used"] = True
    trace.log(session, "state", f"{confirmation['kind']} AWAITING_CONFIRMATION -> CANCELLED ({confirmation_id})")
    return True


def new_transaction(session, kind: str, confirmation_id: str) -> dict:
    """
    Create a transaction record with its own transaction_id and Idempotency-Key.

    Why: AD-5 — 1 request = 1 transaction_id + 1 Idempotency-Key.
    Args:
        session (Session): conversation.
        kind (str): "purchase" or "ticket".
        confirmation_id (str): the consumed confirmation.
    Returns:
        dict: transaction record in state CONFIRMED.
    Raises / Errors:
        None
    Side effects:
        Appends to session.transactions; trace event.
    """
    transaction = {"kind": kind, "state": "AWAITING_CONFIRMATION", "confirmation_id": confirmation_id,
                   "transaction_id": new_transaction_id(), "idempotency_key": str(uuid.uuid4()),
                   "msisdn_hash": hash_msisdn(session.msisdn), "created_at": time.time()}
    session.transactions.append(transaction)
    set_state(session, transaction, "CONFIRMED", "button tap")
    return transaction


def execute_purchase(session, confirmation: dict) -> dict:
    """
    Run a confirmed purchase through the ESB with idempotent retries.

    Why: UC3 / UC4 and §2.4 — CONFIRMED -> SUBMITTING -> SUCCEEDED / FAILED_BUSINESS / UNKNOWN -> PENDING_VERIFICATION.
    Args:
        session (Session): conversation.
        confirmation (dict): consumed confirmation with details.offer and payment_method.
    Returns:
        dict: {"outcome": "succeeded" | "failed_business" | "pending_verification" | "locked", ...}
    Raises / Errors:
        None
    Side effects:
        ESB POST /purchase (possibly retried with the same key); state transitions; trace.
    """
    if purchase_locked(session):
        trace.log(session, "guardrail", "Purchase blocked: another purchase is still in progress (lock)")
        return {"outcome": "locked"}
    details = confirmation["details"]
    transaction = new_transaction(session, "purchase", confirmation["confirmation_id"])
    set_state(session, transaction, "SUBMITTING")
    payload = {"purchase": {"offer_id": details["offer"]["offer_id"], "payment_method": details["payment_method"],
                            "confirmation_id": confirmation["confirmation_id"], "is_confirmation": "1"}}
    result = call_esb(session, "purchase", "POST", "/purchase", payload=payload,
                      idempotency_key=transaction["idempotency_key"], transaction_id=transaction["transaction_id"],
                      on_retry=lambda: set_state(session, transaction, "UNKNOWN", "timeout/5xx, retrying with same key"))
    if result["ok"]:
        replay = result["data"]["purchase"].get("idempotent_replay", False)
        set_state(session, transaction, "SUCCEEDED", "idempotent replay of original result" if replay else "")
        if details["payment_method"] == "PAKET_DARURAT":
            session.loans_used += 1
        return {"outcome": "succeeded", "purchase": result["data"]["purchase"], "replay": replay}
    if not result["transient"]:
        set_state(session, transaction, "FAILED_BUSINESS", f"{result['status_code']} {result['status_desc']}")
        return {"outcome": "failed_business", "status_code": result["status_code"]}
    set_state(session, transaction, "PENDING_VERIFICATION", "still unknown after retries; new purchases blocked")
    # TODO[DEFERRED-DEF-02]: Async reconciliation of PENDING_VERIFICATION purchases
    # Why deferred: needs a background job; the lock stays and the customer is handed off
    # Call here when implemented: reconciliation.schedule(transaction_id: str, idempotency_key: str) -> None
    # D2 reference: §2.4 purchase state machine, UC3
    trace.log(session, "deferred", "deferred: async reconciliation skipped (lock kept, handoff)")
    return {"outcome": "pending_verification"}


def execute_ticket(session, confirmation: dict) -> dict:
    """
    Create a confirmed network trouble ticket (1 per conversation) with an Idempotency-Key.

    Why: UC5 — dedup + ticket Idempotency-Key; handoff after repeated ticket failures.
    Args:
        session (Session): conversation.
        confirmation (dict): consumed confirmation with details.symptom.
    Returns:
        dict: {"outcome": "created" | "existing" | "failed", "ticket_no"?, "handoff"?}
    Raises / Errors:
        None
    Side effects:
        ESB POST /ticket; state transitions; may trigger handoff.
    """
    if session.ticket:
        trace.log(session, "guardrail", "Ticket dedup: conversation already has a ticket")
        return {"outcome": "existing", "ticket_no": session.ticket["ticket_no"]}
    transaction = new_transaction(session, "ticket", confirmation["confirmation_id"])
    set_state(session, transaction, "SUBMITTING")
    evidence = {"quota_ok": session.diagnosis["status"] != "QUOTA_EXHAUSTED",
                "fup": session.quota["fup_status"], "network_status": session.network["status"]}
    payload = {"ticket": {"cell_id": session.cell_id, "category": "NETWORK_DEGRADED",
                          "symptom": confirmation["details"]["symptom"], "evidence": evidence}}
    result = call_esb(session, "ticket", "POST", "/ticket", payload=payload,
                      idempotency_key=transaction["idempotency_key"], transaction_id=transaction["transaction_id"])
    if result["ok"]:
        set_state(session, transaction, "SUCCEEDED")
        session.ticket = result["data"]["ticket"]
        return {"outcome": "created", "ticket_no": session.ticket["ticket_no"]}
    set_state(session, transaction, "FAILED_BUSINESS" if not result["transient"] else "FAILED",
              f"{result['status_code']} {result['status_desc']}")
    session.ticket_failures += 1
    if session.ticket_failures >= rule("handoff", "ticket_failures_before_handoff"):
        return {"outcome": "failed", "handoff": guardrails.do_handoff(session, "ticket_failed_twice")}
    return {"outcome": "failed"}
