"""
File: core/trace.py
Purpose: Structured, masked, timestamped trace of every agent step, plus the
         per-conversation cost meter (actual + projected production cost).
D2 component: Trace logger (§2.2) and Cost meter (§2.9)
Depends on: core/config.py, core/privacy.py
"""

import logging
import time

from core.config import SETTINGS
from core.privacy import mask_data

logger = logging.getLogger("asisten_kuota.trace")


def log(session, event_type: str, summary: str, **data) -> dict:
    """
    Append one masked trace event to the session and write it to the server log.

    Why: D4 requires a log/trace of every agent step; §2.8 requires it masked.
    Args:
        session (Session): conversation the event belongs to.
        event_type (str): e.g. "llm_call", "tool_call", "esb_call", "esb_retry", "state", "guardrail", "cost", "deferred".
        summary (str): one-line human-readable description.
        **data: structured details (masked before storing).
    Returns:
        dict: the stored event.
    Raises / Errors:
        None
    Side effects:
        Appends to session.trace; writes one log line.
    """
    # TODO[DEFERRED-DEF-03]: Retention purge (trace 30 days, transactions/keys 24 h)
    # Why deferred: trace is in-memory and cleared on restart; policy is documented in README
    # Call here when implemented: purge_expired(store, retention_days: int) -> int
    # D2 reference: §2.8 retention
    event = {
        "seq": len(session.trace) + 1,
        "ts": time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
        "type": event_type,
        "summary": mask_data(summary),
        "data": mask_data(data),
    }
    session.trace.append(event)
    logger.info("[%s] #%s %s %s", session.session_id, event["seq"], event_type, event["summary"])
    return event


def estimate_tokens(text: str) -> int:
    """
    Rough token estimate (about 4 characters per token).

    Why: §2.11 — when a provider returns no usage, estimate and mark as estimated.
    Args:
        text (str): serialized prompt or completion.
    Returns:
        int: estimated token count (at least 1).
    Raises / Errors:
        None
    Side effects:
        None
    """
    return max(1, len(text) // 4)


def add_llm_usage(session, prompt_tokens: int, completion_tokens: int, estimated: bool) -> dict:
    """
    Add one LLM call's tokens to the cost meter and recompute costs in Rupiah.

    Why: §2.9 — projected production cost uses GPT-4o-mini prices from .env on every
         provider, so the Rp 250 cap stays demonstrable on a free tier.
    Args:
        session (Session): conversation.
        prompt_tokens (int): input tokens of this call.
        completion_tokens (int): output tokens of this call.
        estimated (bool): True if the tokens were estimated (no usage from provider).
    Returns:
        dict: the updated session.cost.
    Raises / Errors:
        None
    Side effects:
        Updates session.cost; writes a "cost" trace event.
    """
    llm = SETTINGS["llm"]
    cost = session.cost
    cost["prompt_tokens"] += prompt_tokens
    cost["completion_tokens"] += completion_tokens
    cost["estimated"] = cost["estimated"] or estimated
    usd = (cost["prompt_tokens"] * llm["price_input_usd_per_1m"]
           + cost["completion_tokens"] * llm["price_output_usd_per_1m"]) / 1_000_000
    cost["projected_idr"] = round(usd * llm["fx_idr_per_usd"], 2)
    # Free tier / mock: real cost is Rp 0; paid (Azure): real cost = projected cost.
    cost["actual_idr"] = cost["projected_idr"] if llm["billing"] == "paid" else 0.0
    log(session, "cost", f"Projected cost Rp {cost['projected_idr']:.2f} (actual Rp {cost['actual_idr']:.2f})",
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, estimated=estimated)
    return cost
