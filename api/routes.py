"""
File: api/routes.py
Purpose: Our backend API for the chat UI: login (persona), consent, chat turn,
         confirm/cancel buttons, CSAT, handoff, trace and reset.
D2 component: Conversation API and session (§2.2)
Depends on: flask, requests, core/*
"""

import json

import requests
from flask import Blueprint, jsonify, request

from core import agent, guardrails, trace, transactions
from core.config import ROOT_DIR, SETTINGS, rule
from core.esb_adapter import call_esb
from core.mock_llm import rupiah
from core.session import clear_sessions, create_session, get_session
from core.tools import tool_get_quota

api = Blueprint("api", __name__, url_prefix="/api")

GREETING = ("Halo! Saya Asisten Kuota. Saya bisa cek kuota dan jaringan, merekomendasikan paket, "
            "dan membantu pembelian setelah Anda konfirmasi. Ada kendala apa hari ini?")
CONSENT_TEXT = ("Sebelum mulai: Asisten Kuota memakai AI untuk memproses data kuota, pulsa, dan status jaringan Anda. "
                "Data pribadi disamarkan dan tidak dibagikan ke pihak lain. Apakah Anda setuju?")


def load_personas() -> list:
    """
    Read the simulated-login personas.

    Why: session = selected persona (simulates MyTelkomsel login).
    Args:
        None
    Returns:
        list: persona dicts {persona_id, label, msisdn, device_cell_id}.
    Raises / Errors:
        FileNotFoundError: if the data file is missing.
    Side effects:
        None
    """
    with open(ROOT_DIR / "data" / "personas_login.json", encoding="utf-8") as personas_file:
        return json.load(personas_file)["personas"]


def respond(session, replies: list, cards: list | None = None):
    """
    Build the standard JSON response for the UI.

    Why: one response shape for every endpoint (replies, cards, cost, provider, ended).
    Args:
        session (Session): conversation.
        replies (list): assistant texts (Bahasa Indonesia).
        cards (list | None): UI cards.
    Returns:
        flask.Response: JSON.
    Raises / Errors:
        None
    Side effects:
        None
    """
    llm = SETTINGS["llm"]
    return jsonify({
        "session_id": session.session_id,
        "replies": [reply for reply in replies if reply],
        "cards": cards or [],
        "cost": {**{k: session.cost[k] for k in ("projected_idr", "actual_idr", "prompt_tokens", "completion_tokens", "estimated")},
                 "warn_idr": rule("cost", "warn_idr"), "cap_idr": rule("cost", "cap_idr")},
        "provider": f"{llm['provider']}/{llm['model']}",
        "ended": session.ended,
    })


def session_or_404():
    """
    Load the session named in the JSON body.

    Why: every action is scoped to one conversation.
    Args:
        None (reads request JSON "session_id").
    Returns:
        tuple: (Session | None, error response | None)
    Raises / Errors:
        None
    Side effects:
        None
    """
    session = get_session((request.get_json(silent=True) or {}).get("session_id", ""))
    if session is None:
        return None, (jsonify({"error": "unknown session"}), 404)
    return session, None


@api.get("/personas")
def personas():
    """
    GET /api/personas — personas for the login selector (masked MSISDN only).

    Why: simulated login; the browser never needs the full MSISDN.
    Args:
        None
    Returns:
        JSON list of {persona_id, label}.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return jsonify([{"persona_id": p["persona_id"], "label": p["label"]} for p in load_personas()])


@api.post("/session")
def start_session():
    """
    POST /api/session {persona_id} — start a conversation and run the consent check.

    Why: UC1 — get_profile (system call, never an LLM tool) → consent check.
    Args:
        JSON {persona_id}
    Returns:
        Standard response with greeting, consent card or non-AI message.
    Raises / Errors:
        404 for an unknown persona.
    Side effects:
        Creates a session; ESB GET /profile; trace events.
    """
    persona_id = (request.get_json(silent=True) or {}).get("persona_id")
    persona = next((p for p in load_personas() if p["persona_id"] == persona_id), None)
    if persona is None:
        return jsonify({"error": "unknown persona"}), 404
    session = create_session(persona["persona_id"], persona["msisdn"], persona["device_cell_id"])
    llm = SETTINGS["llm"]
    trace.log(session, "session", f"Login simulated as {persona_id}; LLM provider {llm['provider']}/{llm['model']}",
              msisdn=session.msisdn, cell_id=session.cell_id, pseudonym=session.pseudonym)
    result = call_esb(session, "profile", "GET", f"/subscriber/{session.msisdn}/profile")
    if not result["ok"]:
        session.ended = True
        return respond(session, ["Maaf, data pelanggan belum bisa dimuat. Silakan coba lagi."])
    profile = result["data"]["profile"]
    session.segment = profile["segment"]
    session.consent = guardrails.consent_status(profile)
    trace.log(session, "guardrail", f"Consent check at conversation start: {session.consent}")
    if session.consent == "granted":
        return respond(session, [GREETING])
    if session.consent == "refused":
        session.ended = True
        return respond(session, [guardrails.REFUSED_CONSENT_TEXT])
    return respond(session, [CONSENT_TEXT], [{"type": "consent"}])


@api.post("/consent")
def consent():
    """
    POST /api/consent {session_id, decision: "granted" | "refused"} — consent buttons.

    Why: UU PDP — refusal means no AI processing (non-AI menu / human).
    Args:
        JSON {session_id, decision}
    Returns:
        Standard response.
    Raises / Errors:
        404 unknown session.
    Side effects:
        Records consent; may end the session.
    """
    session, error = session_or_404()
    if error:
        return error
    decision = "granted" if (request.get_json() or {}).get("decision") == "granted" else "refused"
    guardrails.record_consent(session, decision)
    if decision == "refused":
        session.ended = True
        return respond(session, [guardrails.REFUSED_CONSENT_TEXT])
    return respond(session, ["Terima kasih atas persetujuannya. " + GREETING])


@api.post("/chat")
def chat():
    """
    POST /api/chat {session_id, message} — one agent turn.

    Why: UC1–UC5 entry point. A typed "ya" is just text; it can never confirm a purchase.
    Args:
        JSON {session_id, message}
    Returns:
        Standard response with replies and cards.
    Raises / Errors:
        404 unknown session.
    Side effects:
        See core.agent.run_turn.
    """
    session, error = session_or_404()
    if error:
        return error
    message = str((request.get_json() or {}).get("message", ""))[:1000]
    with session.lock:                       # one turn at a time per conversation
        result = agent.run_turn(session, message)
    return respond(session, result["replies"], result["cards"])


def purchase_reply(session, confirmation: dict, outcome: dict) -> tuple:
    """
    Turn a purchase outcome into a deterministic customer message (no LLM needed).

    Why: UC3 — success is verified with get_quota; failures get clear messages (§11 failure messaging).
    Args:
        session (Session): conversation.
        confirmation (dict): consumed confirmation.
        outcome (dict): result of transactions.execute_purchase.
    Returns:
        tuple: (replies list, cards list)
    Raises / Errors:
        None
    Side effects:
        ESB GET /quota on success; may hand off.
    """
    offer = confirmation["details"]["offer"]
    if outcome["outcome"] == "succeeded":
        tool_get_quota(session, {})          # verify activation (UC3) and refresh pulsa
        quota_gb = session.quota["internet_remaining_mb"] / 1024
        text = (f"Paket {offer['name']} sudah aktif. Kuota internet Anda sekarang {quota_gb:.1f} GB, "
                f"sisa pulsa {rupiah(session.quota['pulsa_idr'])}.")
        return [text], [{"type": "result", "ok": True, "text": text}, {"type": "csat"}]
    if outcome["outcome"] == "locked":
        return ["Pembelian sebelumnya masih diproses. Mohon tunggu, saya tidak akan membeli dua kali."], []
    if outcome["outcome"] == "failed_business":
        if outcome["status_code"] == "30101":
            return ["Maaf, pulsa Anda tidak cukup untuk paket ini. Coba paket yang lebih murah atau isi ulang pulsa."], []
        return ["Maaf, paket ini sudah tidak tersedia untuk Anda. Silakan pilih paket lain."], []
    card = guardrails.do_handoff(session, "purchase_pending_verification")
    text = ("Status pembelian belum bisa dipastikan. Untuk mencegah tagihan ganda, saya tidak akan mengulang pembelian. "
            + card["message"])
    return [text], [card]


def ticket_reply(session, outcome: dict) -> tuple:
    """
    Turn a ticket outcome into a deterministic customer message.

    Why: UC5 — ticket number shown; handoff after repeated failure.
    Args:
        session (Session): conversation.
        outcome (dict): result of transactions.execute_ticket.
    Returns:
        tuple: (replies list, cards list)
    Raises / Errors:
        None
    Side effects:
        None
    """
    if outcome["outcome"] in ("created", "existing"):
        text = (f"Laporan gangguan sudah dibuat dengan nomor {outcome['ticket_no']}. Tim jaringan sedang menangani "
                "gangguan di area Anda. Kuota Anda tidak berkurang karena gangguan ini.")
        return [text], [{"type": "result", "ok": True, "text": text}, {"type": "csat"}]
    if outcome.get("handoff"):
        return [outcome["handoff"]["message"]], [outcome["handoff"]]
    return ["Maaf, laporan belum berhasil dibuat. Anda bisa minta saya mencoba lagi."], []


@api.post("/confirm")
def confirm():
    """
    POST /api/confirm {session_id, confirmation_id} — the ONLY way a purchase or ticket executes.

    Why: AD-4 confirmation gate; single-use confirmation; anti double-purchase.
    Args:
        JSON {session_id, confirmation_id}
    Returns:
        Standard response with the outcome.
    Raises / Errors:
        404 unknown session.
    Side effects:
        ESB purchase/ticket calls; state transitions; history update.
    """
    session, error = session_or_404()
    if error:
        return error
    confirmation_id = (request.get_json() or {}).get("confirmation_id", "")
    with session.lock:                       # a double tap waits here, then finds the confirmation used
        confirmation, reason = transactions.consume_confirmation(session, confirmation_id)
        if confirmation is None:
            texts = {"already_used": "Konfirmasi ini sudah diproses sebelumnya.",
                     "expired": "Konfirmasi sudah kedaluwarsa (lebih dari 5 menit). Silakan pilih paket lagi.",
                     "unknown": "Konfirmasi tidak ditemukan."}
            return respond(session, [texts[reason]])
        if confirmation["kind"] == "purchase":
            replies, cards = purchase_reply(session, confirmation, transactions.execute_purchase(session, confirmation))
        else:
            replies, cards = ticket_reply(session, transactions.execute_ticket(session, confirmation))
        session.messages.append({"role": "assistant", "content": " ".join(replies)})   # keep the LLM informed
    return respond(session, replies, cards)


@api.post("/cancel")
def cancel():
    """
    POST /api/cancel {session_id, confirmation_id} — the [Batal] button.

    Why: §2.4 AWAITING_CONFIRMATION -> CANCELLED.
    Args:
        JSON {session_id, confirmation_id}
    Returns:
        Standard response.
    Raises / Errors:
        404 unknown session.
    Side effects:
        Marks the confirmation used; trace.
    """
    session, error = session_or_404()
    if error:
        return error
    cancelled = transactions.cancel_confirmation(session, (request.get_json() or {}).get("confirmation_id", ""))
    text = "Baik, dibatalkan. Tidak ada pembelian atau laporan yang dibuat." if cancelled else "Tidak ada yang perlu dibatalkan."
    session.messages.append({"role": "assistant", "content": text})
    return respond(session, [text])


@api.post("/csat")
def csat():
    """
    POST /api/csat {session_id, score} — CSAT 1–5 (SIMPLIFIED: written to the trace only).

    Why: KPI CSAT >= 4.2.
    Args:
        JSON {session_id, score}
    Returns:
        Standard response.
    Raises / Errors:
        404 unknown session.
    Side effects:
        Trace event.
    """
    session, error = session_or_404()
    if error:
        return error
    score = int((request.get_json() or {}).get("score", 0))
    trace.log(session, "csat", f"CSAT score {score}/5")
    return respond(session, ["Terima kasih atas penilaiannya!"])


@api.post("/handoff")
def handoff():
    """
    POST /api/handoff {session_id} — the [Hubungi petugas] button on fallback messages.

    Why: §2.11 — after LLM failure / rate limit, offer a human.
    Args:
        JSON {session_id}
    Returns:
        Standard response with the handoff card.
    Raises / Errors:
        404 unknown session.
    Side effects:
        Ends AI processing.
    """
    session, error = session_or_404()
    if error:
        return error
    card = guardrails.do_handoff(session, "customer_pressed_contact_agent")
    return respond(session, [card["message"]], [card])


@api.get("/trace/<session_id>")
def get_trace(session_id):
    """
    GET /api/trace/<session_id> — masked trace events, cost and provider.

    Why: D4 trace panel.
    Args:
        session_id (str): path parameter.
    Returns:
        JSON {events, cost, provider}.
    Raises / Errors:
        404 unknown session.
    Side effects:
        None
    """
    session = get_session(session_id)
    if session is None:
        return jsonify({"error": "unknown session"}), 404
    llm = SETTINGS["llm"]
    return jsonify({"events": session.trace, "cost": session.cost, "provider": f"{llm['provider']}/{llm['model']}"})


@api.post("/reset")
def reset():
    """
    POST /api/reset — clear all sessions and reseed the mock ESB.

    Why: repeatable demos.
    Args:
        None
    Returns:
        JSON {"reset": true, "esb_reset": bool}
    Raises / Errors:
        None
    Side effects:
        Clears sessions; calls the mock-only /esb/_admin/reset.
    """
    clear_sessions()
    try:
        requests.post(rule("esb", "base_url") + "/_admin/reset", timeout=3)
        esb_reset = True
    except requests.RequestException:
        esb_reset = False
    return jsonify({"reset": True, "esb_reset": esb_reset})
