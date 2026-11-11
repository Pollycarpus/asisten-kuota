"""
File: core/guardrails.py
Purpose: Deterministic policy checks that do NOT depend on the LLM: consent,
         message screening (human request, anger, prompt injection), offer whitelist,
         Paket Darurat policy, cost guard and handoff.
D2 component: Policy and guardrail engine (§2.2, AD-2, §2.10)
Depends on: core/config.py, core/trace.py, core/privacy.py
"""

import re
import uuid

from core import trace
from core.config import rule
from core.privacy import mask_msisdn

DEESCALATION_TEXT = ("Mohon maaf atas ketidaknyamanannya, saya mengerti ini menjengkelkan. "
                     "Saya akan bantu secepatnya.")
INJECTION_REFUSAL_TEXT = ("Maaf, saya hanya bisa membantu soal kuota, paket, pulsa, dan gangguan jaringan "
                          "MyTelkomsel. Ada yang bisa saya bantu terkait itu?")
REFUSED_CONSENT_TEXT = ("Baik, Asisten Kuota tidak akan memproses data Anda dengan AI. "
                        "Anda tetap bisa memakai menu non-AI: Beli Paket, Cek Kuota, atau hubungi Call Center 188.")


def consent_status(profile: dict) -> str:
    """
    Read the AI-assistant consent from the ESB profile.

    Why: UU PDP — consent is checked at the start of every conversation; refusal = no AI processing.
    Args:
        profile (dict): profile section from GET /subscriber/{msisdn}/profile.
    Returns:
        str: "granted", "refused" or "not_asked".
    Raises / Errors:
        None
    Side effects:
        None
    """
    value = (profile.get("consent") or {}).get("ai_assistant")
    if value is True:
        return "granted"
    if value is False:
        return "refused"
    return "not_asked"


def record_consent(session, decision: str) -> None:
    """
    Record the customer's consent decision for this conversation.

    Why: UU PDP consent; persisting it via the ESB is DEFERRED (no write endpoint in the brief's contract).
    Args:
        session (Session): conversation.
        decision (str): "granted" or "refused".
    Returns:
        None
    Raises / Errors:
        None
    Side effects:
        Sets session.consent; trace events.
    """
    session.consent = decision
    trace.log(session, "guardrail", f"Consent {decision} by customer (button)")
    # TODO[DEFERRED-DEF-10]: Persist consent via ESB (granted_at, version)
    # Why deferred: the brief's API contract has no consent-write endpoint
    # Call here when implemented: esb_adapter.call_esb(session, "consent", "POST", "/subscriber/{msisdn}/consent", payload=...)
    # D2 reference: §2.8 consent
    trace.log(session, "deferred", "deferred: consent persistence skipped (session only)")


def _matches_any(text: str, words: list) -> bool:
    """
    True if any keyword occurs in the text as a whole word (case-insensitive).

    Why: keyword-based detection must not fire inside other words (e.g. "cs" in "discs").
    Args:
        text (str): customer message.
        words (list): keywords or phrases.
    Returns:
        bool
    Raises / Errors:
        None
    Side effects:
        None
    """
    lowered = text.lower()
    return any(re.search(r"\b" + re.escape(word.lower()) + r"\b", lowered) for word in words)


def screen_message(text: str) -> dict:
    """
    Classify a customer message: asks for a human, angry, or prompt injection.

    Why: §2.10 handoff triggers and basic injection filter (SIMPLIFIED, deterministic keywords from config).
    Args:
        text (str): raw customer message.
    Returns:
        dict: {"wants_human": bool, "angry": bool, "injection": bool}
    Raises / Errors:
        None
    Side effects:
        None
    """
    # TODO[DEFERRED-DEF-11]: LLM-based sentiment / injection classifier
    # Why deferred: keyword rules are deterministic, testable and explainable for the demo
    # Call here when implemented: classify_message(text: str) -> {"angry": float, "injection": float}
    # D2 reference: §2.2 policy and guardrail engine
    exclamations = "!" * rule("handoff", "anger_exclamation_run")
    injection = any(re.search(pattern, text, re.IGNORECASE) for pattern in rule("guardrails", "injection_patterns"))
    return {
        "wants_human": _matches_any(text, rule("handoff", "human_keywords")),
        "angry": _matches_any(text, rule("handoff", "anger_keywords")) or exclamations in text,
        "injection": injection,
    }


def is_whitelisted(session, offer_id: str) -> dict | None:
    """
    Return the offer if it is in this conversation's Top-N list, else None.

    Why: UC2 — the LLM may only pick from the Top-N (offer whitelist).
    Args:
        session (Session): conversation with session.top_offers.
        offer_id (str): ID chosen by the LLM.
    Returns:
        dict | None: the whitelisted offer.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return next((offer for offer in session.top_offers if offer["offer_id"] == offer_id), None)


def check_reply_offers(session, reply_text: str) -> bool:
    """
    True if every offer ID mentioned in the LLM's reply is whitelisted.

    Why: UC2 — validate offer IDs in LLM output against the whitelist (no invented packages).
    Args:
        session (Session): conversation.
        reply_text (str): assistant text.
    Returns:
        bool: False if the reply mentions a non-whitelisted offer ID.
    Raises / Errors:
        None
    Side effects:
        None
    """
    mentioned = re.findall(r"BID-\d+", reply_text or "")
    allowed = {offer["offer_id"] for offer in session.top_offers}
    return all(offer_id in allowed for offer_id in mentioned)


def paket_darurat_policy(session, offer: dict) -> tuple:
    """
    Check the agent-side Paket Darurat policy before calling the ESB.

    Why: UC4 — what the agent is allowed to offer comes from config.toml [paket_darurat].
    Args:
        session (Session): conversation (pulsa, loans used).
        offer (dict): whitelisted offer.
    Returns:
        tuple: (allowed: bool, reason: str)
    Raises / Errors:
        None
    Side effects:
        None
    """
    policy = rule("paket_darurat")
    pulsa = (session.quota or {}).get("pulsa_idr", 0)
    if not policy["enabled"]:
        return False, "paket_darurat_disabled"
    if policy["offer_when_pulsa_below_price"] and offer["price_idr"] <= pulsa:
        return False, "pulsa_sufficient_use_pulsa"
    if offer["price_idr"] > policy["max_offer_price_idr"]:
        return False, "price_above_policy_max"
    if offer["category"] not in policy["allowed_categories"]:
        return False, "category_not_allowed"
    if session.loans_used >= policy["max_loans_per_conversation"]:
        return False, "loan_limit_per_conversation_reached"
    return True, "allowed"


def cost_status(session) -> str:
    """
    Compare the projected cost with the warn and cap thresholds.

    Why: §1.4 — LLM cost max Rp 250 per conversation; warn at Rp 200.
    Args:
        session (Session): conversation with session.cost.
    Returns:
        str: "ok", "warn" or "cap".
    Raises / Errors:
        None
    Side effects:
        Writes a guardrail trace event the first time "warn" is reached.
    """
    projected = session.cost["projected_idr"]
    if projected >= rule("cost", "cap_idr"):
        return "cap"
    if projected >= rule("cost", "warn_idr"):
        if not session.cost["warned"]:
            session.cost["warned"] = True
            trace.log(session, "guardrail", f"Cost warning: Rp {projected:.2f} >= Rp {rule('cost', 'warn_idr')}")
        return "warn"
    return "ok"


def build_handoff_summary(session, reason: str) -> dict:
    """
    Build the masked summary a human agent receives.

    Why: §2.8 — human handoff receives a masked summary + ref ID only.
    Args:
        session (Session): conversation.
        reason (str): handoff trigger.
    Returns:
        dict: masked summary (no name, no full MSISDN, no cell ID).
    Raises / Errors:
        None
    Side effects:
        None
    """
    return {
        "customer": session.pseudonym,
        "msisdn_masked": mask_msisdn(session.msisdn),
        "segment": session.segment,
        "reason": reason,
        "diagnosis": (session.diagnosis or {}).get("status"),
        "transactions": [{"kind": t["kind"], "state": t["state"]} for t in session.transactions],
        "ticket_no": (session.ticket or {}).get("ticket_no"),
        "anger_count": session.anger_count,
    }


def do_handoff(session, reason: str) -> dict:
    """
    Hand the conversation to a human agent and stop AI processing.

    Why: §2.10 handoff rules (asks for human, persistent anger, cost cap, stuck purchase, ticket failures).
    Args:
        session (Session): conversation.
        reason (str): trigger, e.g. "customer_requested_human".
    Returns:
        dict: UI card {type: "handoff", ref_id, summary, message}.
    Raises / Errors:
        None
    Side effects:
        Sets session.ended and session.handoff; trace events.
    """
    ref_id = "HO-" + uuid.uuid4().hex[:8].upper()
    summary = build_handoff_summary(session, reason)
    # TODO[DEFERRED-DEF-07]: Send the masked summary to a real human-agent queue
    # Why deferred: no agent desktop in the prototype; the summary is shown in the UI and trace
    # Call here when implemented: agent_queue.enqueue(ref_id: str, summary: dict) -> None
    # D2 reference: §2.10 handoff
    trace.log(session, "deferred", "deferred: human agent queue skipped (summary shown in UI)")
    session.ended = True
    session.handoff = {"ref_id": ref_id, "reason": reason, "summary": summary}
    trace.log(session, "guardrail", f"Handoff to human: {reason} (ref {ref_id})", handoff_summary=summary)
    message = (f"Saya hubungkan Anda dengan petugas kami. Nomor referensi: {ref_id}. "
               f"{rule('handoff', 'sla_text')} Ringkasan percakapan sudah kami teruskan, jadi Anda tidak perlu mengulang.")
    return {"type": "handoff", "ref_id": ref_id, "summary": summary, "message": message}


def extra_handoff_checks(session) -> str | None:
    """
    Stub for the deferred handoff triggers (misunderstandings, priority segment).

    Why: §2.10 triggers DEF-05 and DEF-06 are DEFERRED; hooks sit where all triggers are evaluated.
    Args:
        session (Session): conversation.
    Returns:
        str | None: always None (no extra trigger).
    Raises / Errors:
        None
    Side effects:
        None
    """
    # TODO[DEFERRED-DEF-05]: Handoff after 2 consecutive misunderstandings
    # Why deferred: needs a reliable "misunderstood" signal (LLM judge or intent confidence)
    # Call here when implemented: count_misunderstandings(session) -> int
    # D2 reference: §2.10 handoff rules
    # TODO[DEFERRED-DEF-06]: Handoff for priority segment
    # Why deferred: no demo persona is in a priority segment
    # Call here when implemented: is_priority_segment(segment: str) -> bool
    # D2 reference: §2.10 handoff rules
    return None
