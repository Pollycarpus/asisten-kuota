"""
File: tests/conftest.py
Purpose: Shared test setup: force the mock LLM, start the mock ESB on a free port
         over real HTTP, shorten timeouts, and reset all state before each test.
D2 component: Scenario tests (§7 step 9)
Depends on: pytest, app.py, core/config.py
"""

import os
import socket
import sys
from pathlib import Path

import pytest
import requests

os.environ["LLM_PROVIDER"] = "mock"            # tests never need a key or network
os.environ["LLM_MODEL"] = "mock-scripted"
os.environ["LLM_BILLING"] = "free"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module                         # noqa: E402  (after env setup)
from core.config import SETTINGS                 # noqa: E402
from core.session import clear_sessions          # noqa: E402

# Fast but faithful timing: purchase timeout < mock delay, so the P5 timeout still happens.
FAST_RULES = {
    ("esb", "timeouts_s", "purchase"): 1.0,
    ("mock_esb", "purchase_timeout_delay_s"): 1.5,
    ("esb", "retry", "write_backoff_s"): [0.2, 0.4],
    ("mock_esb", "quota_latency_ms"): [200, 300],
}


def free_port() -> int:
    """
    Find a free local TCP port.

    Why: tests must not clash with a running demo on 8000/8001.
    Args:
        None
    Returns:
        int: port number.
    Raises / Errors:
        None
    Side effects:
        Opens and closes a socket.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def set_rule(path: tuple, value) -> None:
    """
    Override one config.toml value in memory for the tests.

    Why: tests change rules the same way an operator would (config, not code).
    Args:
        path (tuple): section/key path.
        value (Any): new value.
    Returns:
        None
    Raises / Errors:
        KeyError: unknown path.
    Side effects:
        Mutates SETTINGS["rules"].
    """
    node = SETTINGS["rules"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@pytest.fixture(scope="session")
def esb_url():
    """
    Start the mock ESB once for the whole test run, on a free port.

    Why: the adapter must talk to the ESB over real HTTP (real timeouts and retries).
    Args:
        None
    Returns:
        str: ESB base URL.
    Raises / Errors:
        None
    Side effects:
        Starts a background server thread.
    """
    port = free_port()
    server = app_module.start_server(app_module.create_esb_app(), port, "test-esb")
    url = f"http://127.0.0.1:{port}/esb"
    set_rule(("esb", "base_url"), url)
    yield url
    server.shutdown()


@pytest.fixture()
def client(esb_url):
    """
    Flask test client for our API, with fresh state and fast timings.

    Why: every scenario starts from the seed data.
    Args:
        esb_url (str): fixture.
    Returns:
        FlaskClient: test client.
    Raises / Errors:
        None
    Side effects:
        Resets sessions and mock ESB data; restores the original rules afterwards.
    """
    import copy
    original_rules = copy.deepcopy(SETTINGS["rules"])
    for path, value in FAST_RULES.items():
        set_rule(path, value)
    clear_sessions()
    requests.post(esb_url + "/_admin/reset", timeout=3)
    yield app_module.create_app().test_client()
    SETTINGS["rules"].clear()
    SETTINGS["rules"].update(original_rules)


@pytest.fixture()
def ledger(esb_url):
    """
    Return a function that lists real charges in the mock ESB.

    Why: proves "never purchase twice" from the ESB side, not just from our own state.
    Args:
        esb_url (str): fixture.
    Returns:
        callable: ledger(msisdn=None) -> list of charges.
    Raises / Errors:
        None
    Side effects:
        None
    """
    def _ledger(msisdn=None):
        """
        List charges, optionally for one MSISDN.

        Why: assertion helper for single-charge checks.
        Args:
            msisdn (str | None): filter.
        Returns:
            list: charges.
        Raises / Errors:
            None
        Side effects:
            One HTTP call to the mock-only ledger endpoint.
        """
        params = {"msisdn": msisdn} if msisdn else {}
        return requests.get(esb_url + "/_admin/ledger", params=params, timeout=3).json()["charges"]
    return _ledger


class Chat:
    """
    Tiny helper to drive one conversation through the API in tests.

    Why: keeps scenario tests short and readable.
    Args:
        client (FlaskClient): test client.
        persona_id (str): P1..P5.
    Returns:
        n/a
    Raises / Errors:
        None
    Side effects:
        API calls.
    """

    def __init__(self, client, persona_id):
        """
        Start a session for the persona.

        Why: simulated login.
        Args:
            client (FlaskClient), persona_id (str)
        Returns:
            None
        Raises / Errors:
            None
        Side effects:
            POST /api/session.
        """
        self.client = client
        self.last = client.post("/api/session", json={"persona_id": persona_id}).json
        self.sid = self.last["session_id"]

    def post(self, path, **body):
        """
        POST to an API path with this session's ID.

        Why: shared by say(), confirm(), consent().
        Args:
            path (str): API path; **body: extra JSON fields.
        Returns:
            dict: response JSON.
        Raises / Errors:
            None
        Side effects:
            API call.
        """
        self.last = self.client.post(path, json={"session_id": self.sid, **body}).json
        return self.last

    def say(self, text):
        """
        Send a chat message.

        Why: customer turn.
        Args:
            text (str): message.
        Returns:
            dict: response JSON.
        Raises / Errors:
            None
        Side effects:
            POST /api/chat.
        """
        return self.post("/api/chat", message=text)

    def card(self, card_type):
        """
        Return the first card of a type from the last response, or None.

        Why: assertions on UI cards.
        Args:
            card_type (str): e.g. "confirmation".
        Returns:
            dict | None
        Raises / Errors:
            None
        Side effects:
            None
        """
        return next((c for c in self.last["cards"] if c["type"] == card_type), None)

    def trace(self):
        """
        Fetch this session's trace events.

        Why: assert that steps are traced and masked.
        Args:
            None
        Returns:
            list: events.
        Raises / Errors:
            None
        Side effects:
            GET /api/trace.
        """
        return self.client.get(f"/api/trace/{self.sid}").json["events"]


@pytest.fixture()
def chat(client):
    """
    Factory fixture: chat("P1") -> Chat.

    Why: readable scenario tests.
    Args:
        client (FlaskClient): fixture.
    Returns:
        callable
    Raises / Errors:
        None
    Side effects:
        None
    """
    return lambda persona_id: Chat(client, persona_id)
