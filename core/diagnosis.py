"""
File: core/diagnosis.py
Purpose: Deterministic diagnosis rules engine and Top-N offer ranking.
         The LLM only explains the result (AD-3).
D2 component: Diagnosis rules engine (§2.2, UC1, UC2)
Depends on: core/config.py
"""

from core.config import rule

QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
FUP_THROTTLED = "FUP_THROTTLED"
NETWORK_DEGRADED = "NETWORK_DEGRADED"
NO_ISSUE = "NO_ISSUE"


def diagnose(quota: dict, network: dict) -> dict:
    """
    Classify the customer's condition from quota, FUP and network status.

    Why: AD-3 — diagnosis is rules, not LLM. Priority: quota → FUP → network → no issue.
         "Lambat" with FUP throttled is NOT a network fault (avoids false network tickets).
    Args:
        quota (dict): {internet_remaining_mb, fup_status, pulsa_idr, ...} from get_quota.
        network (dict): {status: "normal" | "degraded", incident_ref} from get_network_status.
    Returns:
        dict: {status, network_degraded (bool), reason (str)}
    Raises / Errors:
        KeyError: if a required field is missing.
    Side effects:
        None
    """
    network_degraded = network["status"] == "degraded"
    if quota["internet_remaining_mb"] <= rule("diagnosis", "quota_exhausted_mb"):
        status, reason = QUOTA_EXHAUSTED, "internet quota at or below threshold"
    elif quota["fup_status"] == "THROTTLED":
        status, reason = FUP_THROTTLED, "fair usage policy throttling active"
    elif network_degraded:
        status, reason = NETWORK_DEGRADED, "quota and FUP normal, cell degraded"
    else:
        status, reason = NO_ISSUE, "quota, FUP and network all normal"
    return {"status": status, "network_degraded": network_degraded, "reason": reason}


def offer_sort_key(offer: dict, preferred: list, pulsa_idr: int) -> tuple:
    """
    Sort key for one offer: fitting category first, then affordable, then cheapest.

    Why: UC2 ranking rule (SIMPLIFIED, no scoring model). Python sorts False before True.
    Args:
        offer (dict): one offer.
        preferred (list): categories that fit the diagnosis.
        pulsa_idr (int): current pulsa.
    Returns:
        tuple: (not fitting, not affordable, price)
    Raises / Errors:
        None
    Side effects:
        None
    """
    return (offer["category"] not in preferred,      # fitting category first
            offer["price_idr"] > pulsa_idr,           # affordable first
            offer["price_idr"])                       # then cheapest


def rank_offers(offers: list, diagnosis: dict, pulsa_idr: int) -> list:
    """
    Rank eligible offers and return the Top-N, each with an `affordable` flag.

    Why: UC2 — the LLM may only pick from this list (whitelist). Ranking (SIMPLIFIED):
         fitting category first, then affordable against pulsa, then cheapest.
    Args:
        offers (list): offers from GET /offers.
        diagnosis (dict): output of diagnose().
        pulsa_idr (int): current pulsa.
    Returns:
        list: Top-N offer dicts with "affordable" added; [] when the network is degraded.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if diagnosis["network_degraded"]:
        return []   # upsell suppression: never sell to a customer with a network fault
    preferred = rule("diagnosis", "preferred_categories").get(diagnosis["status"], [])
    ranked = sorted(offers, key=lambda offer: offer_sort_key(offer, preferred, pulsa_idr))
    ranked = ranked[: rule("diagnosis", "top_n_offers")]
    return [{**offer, "affordable": offer["price_idr"] <= pulsa_idr} for offer in ranked]
