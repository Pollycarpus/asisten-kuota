"""
File: tests/test_idempotency.py
Purpose: Prove idempotency at the ESB contract level: same key + same payload =
         replay with no new charge; same key + different payload = 409/30109.
D2 component: Mock ESB idempotency + ESB adapter (§2.7)
Depends on: tests/conftest.py, requests
"""

import requests

MSISDN = "6281200000001"


def purchase_body(offer_id: str) -> dict:
    """
    Build a purchase request body with the ESB envelope.

    Why: exercise the contract directly, without the agent.
    Args:
        offer_id (str): offer to buy.
    Returns:
        dict: request JSON.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return {"transaction": {"transaction_id": "T" * 25, "channel": "zz"}, "service": {"service_id": MSISDN},
            "purchase": {"offer_id": offer_id, "payment_method": "PULSA", "confirmation_id": "CNF-TEST", "is_confirmation": "1"}}


def test_same_key_replays_without_second_charge(client, esb_url, ledger):
    """
    Same Idempotency-Key + same payload twice -> one charge, second response is a replay.

    Why: §2.7 mock server idempotency.
    Args:
        client, esb_url, ledger: fixtures.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        Two ESB calls.
    """
    headers = {"api-key": "dummy", "Idempotency-Key": "key-123"}
    first = requests.post(esb_url + "/purchase", json=purchase_body("BID-1003"), headers=headers, timeout=5).json()
    second = requests.post(esb_url + "/purchase", json=purchase_body("BID-1003"), headers=headers, timeout=5).json()
    assert first["purchase"]["idempotent_replay"] is False
    assert second["purchase"]["idempotent_replay"] is True
    assert second["purchase"]["order_id"] == first["purchase"]["order_id"]
    assert len(ledger(MSISDN)) == 1


def test_same_key_different_payload_conflicts(client, esb_url, ledger):
    """
    Same key, different offer -> 409 / 30109 and no second charge.

    Why: §2.7 — a key can never be reused for a different request.
    Args:
        client, esb_url, ledger: fixtures.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        Two ESB calls.
    """
    headers = {"api-key": "dummy", "Idempotency-Key": "key-456"}
    requests.post(esb_url + "/purchase", json=purchase_body("BID-1003"), headers=headers, timeout=5)
    conflict = requests.post(esb_url + "/purchase", json=purchase_body("BID-1001"), headers=headers, timeout=5)
    assert conflict.status_code == 409
    assert conflict.json()["transaction"]["status_code"] == "30109"
    assert len(ledger(MSISDN)) == 1


def test_missing_key_is_rejected(client, esb_url):
    """
    No Idempotency-Key -> 400 / 20002.

    Why: §2.6 — POST /purchase requires the header.
    Args:
        client, esb_url: fixtures.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        One ESB call.
    """
    response = requests.post(esb_url + "/purchase", json=purchase_body("BID-1003"), headers={"api-key": "dummy"}, timeout=5)
    assert response.status_code == 400 and response.json()["transaction"]["status_code"] == "20002"
