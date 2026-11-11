"""
File: tests/test_paket_darurat.py
Purpose: Prove the Paket Darurat rules follow config.toml: fee type, limit,
         enable switch, and provider eligibility rules (P5 not eligible).
         Expected values are READ from config, so the tests follow any config change.
D2 component: UC4 Paket Darurat (policy in guardrails + loan rules in mock ESB)
Depends on: tests/conftest.py, core/config.py
"""

from core.config import rule
from tests.conftest import set_rule


def test_flat_fee_from_config(chat):
    """
    P3 confirmation shows the flat fee configured in [mock_esb.loan_rules].

    Why: fee transparency must match config (H+2 ready).
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API calls.
    """
    set_rule(("mock_esb", "loan_rules", "fee_type"), "flat")
    c = chat("P3")
    c.say("Kuota habis")
    assert c.card("confirmation")["fee_idr"] == rule("mock_esb", "loan_rules", "fee_flat_idr")


def test_percent_fee_from_config(chat):
    """
    Switching fee_type to "percent" changes the fee to price x fee_percent / 100.

    Why: CONFIG_GUIDE example edit "flat fee -> percentage fee".
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API calls.
    """
    set_rule(("mock_esb", "loan_rules", "fee_type"), "percent")
    c = chat("P3")
    c.say("Kuota habis")
    confirmation = c.card("confirmation")
    expected = round(confirmation["price_idr"] * rule("mock_esb", "loan_rules", "fee_percent") / 100)
    assert confirmation["fee_idr"] == expected


def test_limit_below_price_makes_customer_not_eligible(chat):
    """
    Lowering limit_idr below the package price -> not eligible -> no confirmation card.

    Why: CONFIG_GUIDE example edit "lower the loan limit".
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API calls.
    """
    set_rule(("mock_esb", "loan_rules", "limit_idr"), 5000)   # P3's cheapest offer costs Rp 8.000
    c = chat("P3")
    c.say("Kuota habis")
    assert c.card("confirmation") is None
    assert any("isi ulang" in reply for reply in c.last["replies"])


def test_disabled_never_offers_loan(chat):
    """
    [paket_darurat] enabled = false -> no ESB eligibility call, top-up suggestion instead.

    Why: agent policy switch.
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API calls.
    """
    set_rule(("paket_darurat", "enabled"), False)
    c = chat("P3")
    c.say("Kuota habis")
    assert c.card("confirmation") is None
    assert not any(e["type"] == "esb_call" and "/loan/eligibility" in e["summary"] for e in c.trace())


def test_p5_not_eligible_by_provider_rules(chat):
    """
    P5 (tenure 30 days, outstanding loan) fails the provider rules.

    Why: UC4 "not eligible" branch, decided by [mock_esb.loan_rules].
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API calls.
    """
    from core import tools
    from core.session import get_session
    c = chat("P5")
    c.post("/api/consent", decision="granted")
    c.say("Kuota habis")
    session = get_session(c.sid)
    session.quota["pulsa_idr"] = 0          # force the loan path for the cheapest offer
    result, _ = tools.execute(session, "check_paket_darurat", {"offer_id": "BID-1005"})
    assert result["eligible"] is False
    assert "outstanding_loan" in result["reasons"] and "tenure_too_short" in result["reasons"]
