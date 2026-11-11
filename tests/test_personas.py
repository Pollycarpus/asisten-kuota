"""
File: tests/test_personas.py
Purpose: One end-to-end scenario test per persona (P1–P5), run with LLM_PROVIDER=mock.
D2 component: Scenario tests (§7 step 9, §8 personas)
Depends on: tests/conftest.py
"""


def test_p1_happy_path_requires_button_and_charges_once(chat, ledger):
    """
    P1: diagnose -> recommend -> confirm -> purchase; typed "ya" never buys; double tap blocked.

    Why: happy path + AD-4 confirmation gate + §1.4 no double purchase.
    Args:
        chat, ledger: fixtures.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API and ESB calls.
    """
    c = chat("P1")
    c.say("Kuota saya habis")
    assert c.card("diagnosis")["status"] == "QUOTA_EXHAUSTED"
    offers = c.card("offers")["offers"]
    assert len(offers) == 3 and offers[0]["offer_id"] == "BID-1003"
    c.say("Saya pilih paket Internet 3GB (ID: BID-1003)")
    confirmation = c.card("confirmation")
    assert confirmation["payment_method"] == "PULSA" and confirmation["total_idr"] == 15000
    c.say("ya")                                     # typed consent is NOT a purchase confirmation
    assert ledger("6281200000001") == []
    c.post("/api/confirm", confirmation_id=confirmation["confirmation_id"])
    assert "sudah aktif" in c.last["replies"][0]
    c.post("/api/confirm", confirmation_id=confirmation["confirmation_id"])   # double tap
    assert "sudah diproses" in c.last["replies"][0]
    assert len(ledger("6281200000001")) == 1


def test_p2_fup_recommends_booster_and_no_ticket(chat):
    """
    P2: "lambat" with FUP throttled -> booster recommendation, no network ticket.

    Why: "internet lambat" is often FUP, not network (business problem §1.2).
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API and ESB calls.
    """
    c = chat("P2")
    c.say("Internet saya lambat banget")
    assert c.card("diagnosis")["status"] == "FUP_THROTTLED"
    assert c.card("offers")["offers"][0]["category"] == "booster"
    assert not any(e["type"] == "tool_call" and "create_ticket" in e["summary"] for e in c.trace())


def test_p3_paket_darurat_shows_fee_and_pays_by_loan(chat, ledger):
    """
    P3: pulsa insufficient -> Paket Darurat with transparent fee -> confirm -> loan purchase.

    Why: edge case "insufficient balance" (UC4): never hide the fee, never auto-buy.
    Args:
        chat, ledger: fixtures.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API and ESB calls.
    """
    c = chat("P3")
    c.say("Kuota habis, pulsa tinggal sedikit")
    confirmation = c.card("confirmation")
    assert confirmation["payment_method"] == "PAKET_DARURAT"
    assert confirmation["fee_idr"] > 0 and confirmation["total_idr"] == confirmation["price_idr"] + confirmation["fee_idr"]
    assert ledger("6281200000003") == []            # nothing bought before the button
    c.post("/api/confirm", confirmation_id=confirmation["confirmation_id"])
    charges = ledger("6281200000003")
    assert len(charges) == 1 and charges[0]["payment_method"] == "PAKET_DARURAT"


def test_p4_outage_no_upsell_ticket_then_angry_handoff(chat):
    """
    P4: network degraded -> no offers -> ticket after button -> angry twice -> handoff.

    Why: edge cases "genuine network outage" (UC5) and "angry customer" (§2.10).
    Args:
        chat: fixture.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API and ESB calls.
    """
    c = chat("P4")
    c.say("Sinyal jelek, internet putus-putus")
    assert c.card("diagnosis")["status"] == "NETWORK_DEGRADED"
    assert c.card("offers") is None                  # upsell suppressed
    confirmation = c.card("confirmation")
    assert confirmation["kind"] == "ticket"
    c.post("/api/confirm", confirmation_id=confirmation["confirmation_id"])
    assert "TT-" in c.last["replies"][0]
    c.say("Payah banget!!!")
    assert "Mohon maaf" in c.last["replies"][0] and not c.last["ended"]   # de-escalate once
    c.say("Kalian tidak becus")
    handoff = c.card("handoff")
    assert c.last["ended"] and handoff["ref_id"].startswith("HO-")
    assert handoff["summary"]["msisdn_masked"] == "6281******04"


def test_p5_consent_then_timeout_retry_single_charge(chat, ledger):
    """
    P5: consent flow; purchase times out once; retry with the SAME key; exactly one charge.

    Why: §1.4 never purchase twice; AD-5 idempotent retry; UU PDP consent.
    Args:
        chat, ledger: fixtures.
    Returns:
        None
    Raises / Errors:
        AssertionError on failure.
    Side effects:
        API and ESB calls (one simulated timeout).
    """
    c = chat("P5")
    assert c.card("consent") is not None
    c.say("Kuota habis")
    assert c.card("consent") is not None             # no AI processing before consent
    c.post("/api/consent", decision="granted")
    c.say("Kuota habis")
    assert all(o["offer_id"] != "BID-1002" or not o["affordable"] for o in c.card("offers")["offers"])
    c.say("Saya pilih paket Internet 2GB (ID: BID-1005)")
    c.post("/api/confirm", confirmation_id=c.card("confirmation")["confirmation_id"])
    assert "sudah aktif" in c.last["replies"][0]
    assert len(ledger("6281200000005")) == 1
    events = c.trace()
    purchase_calls = [e for e in events if e["type"] == "esb_call" and "/purchase" in e["summary"]]
    assert len(purchase_calls) == 2 and "timeout" in purchase_calls[0]["summary"]
    assert purchase_calls[0]["data"]["idempotency_key"] == purchase_calls[1]["data"]["idempotency_key"]
    assert any("idempotent replay" in e["summary"] for e in events if e["type"] == "state")
