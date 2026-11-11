"""
File: core/session.py
Purpose: In-memory conversation sessions. A session = one selected persona
         (simulated MyTelkomsel login). The MSISDN lives here and nowhere the LLM can reach.
D2 component: Conversation API and session (§2.2)
Depends on: core/privacy.py
"""

import threading
import time
import uuid
from dataclasses import dataclass, field

from core.privacy import new_pseudonym

SESSIONS = {}   # session_id -> Session (in-memory; cleared on restart / reset)


@dataclass
class Session:
    """
    All state of one conversation.

    Why: AD-4/AD-5 state (confirmations, transactions) and §2.8 pseudonym live per conversation.
    Args:
        persona_id (str), msisdn (str), cell_id (str): from the simulated login.
    Returns:
        n/a (dataclass)
    Raises / Errors:
        None
    Side effects:
        None
    """
    persona_id: str
    msisdn: str
    cell_id: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    pseudonym: str = field(default_factory=new_pseudonym)
    segment: str = ""
    consent: str = "unknown"            # granted | not_asked | refused
    messages: list = field(default_factory=list)       # LLM history (masked)
    quota: dict | None = None           # last get_quota result (numbers only)
    network: dict | None = None         # last get_network_status result
    diagnosis: dict | None = None       # deterministic rules-engine output
    top_offers: list = field(default_factory=list)     # the whitelist (Top-N)
    loan_checks: dict = field(default_factory=dict)    # offer_id -> eligibility result
    loans_used: int = 0
    confirmations: dict = field(default_factory=dict)  # confirmation_id -> pending confirmation
    transactions: list = field(default_factory=list)   # purchase / ticket transactions
    ticket: dict | None = None          # 1 ticket per conversation (dedup)
    ticket_failures: int = 0
    anger_count: int = 0
    ended: bool = False                 # True after handoff or consent refusal
    handoff: dict | None = None
    cost: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0,
                                                "projected_idr": 0.0, "actual_idr": 0.0,
                                                "estimated": False, "warned": False})
    trace: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def create_session(persona_id: str, msisdn: str, cell_id: str) -> Session:
    """
    Create and store a new session for a simulated login.

    Why: the MSISDN comes from the session, never from LLM output or typed text (§2.2).
    Args:
        persona_id (str): P1..P5.
        msisdn (str): from data/personas_login.json.
        cell_id (str): device location reported by the app.
    Returns:
        Session: the new session.
    Raises / Errors:
        None
    Side effects:
        Adds the session to SESSIONS.
    """
    session = Session(persona_id=persona_id, msisdn=msisdn, cell_id=cell_id)
    SESSIONS[session.session_id] = session
    return session


def get_session(session_id: str) -> Session | None:
    """
    Look up a session by ID.

    Why: every API call is scoped to one session.
    Args:
        session_id (str): ID returned by POST /api/session.
    Returns:
        Session | None: the session, or None if unknown.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return SESSIONS.get(session_id)


def clear_sessions() -> None:
    """
    Delete all sessions.

    Why: POST /api/reset for repeatable demos and tests.
    Args:
        None
    Returns:
        None
    Raises / Errors:
        None
    Side effects:
        Empties SESSIONS.
    """
    SESSIONS.clear()
