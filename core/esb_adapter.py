"""
File: core/esb_adapter.py
Purpose: The ONLY way the backend talks to the ESB: builds the envelope and headers,
         applies per-endpoint timeouts, retries transient errors with backoff + jitter,
         reuses the same Idempotency-Key on write retries, and maps status codes.
         The LLM never sees raw ESB payloads (AD-6).
D2 component: ESB adapter (§2.2, §2.5, §2.7)
Depends on: requests, core/config.py, core/trace.py, core/privacy.py
"""

import hashlib
import random
import time
import uuid

import requests

from core import trace
from core.config import rule
from core.privacy import mask_text

# Status codes used in the demo (SIMPLIFIED catalogue, §2.5).
STATUS_MEANING = {
    "00000": "success",
    "20001": "invalid parameter: missing api_key",
    "20002": "invalid parameter (MSISDN / Idempotency-Key / confirmation)",
    "30101": "insufficient pulsa",
    "30102": "offer not eligible",
    "30109": "idempotency key conflict",
    "30201": "catalog unavailable",
    "30301": "not eligible for Paket Darurat",
    "30401": "unknown cell",
    "30501": "ticket creation failed",
    "40000": "ESB internal error",
    "10001": "provider unreachable",
}
# TODO[DEFERRED-DEF-04]: Full ESB status-code catalogue beyond the codes used in the demo
# Why deferred: the demo only exercises the codes above; unknown codes are reported as "unmapped"
# Call here when implemented: describe_status(status_code: str) -> str
# D2 reference: §2.5 status codes

WRITE_ENDPOINTS = {"purchase", "ticket"}   # retries must reuse the same Idempotency-Key


def new_transaction_id() -> str:
    """
    Generate a 25-character ESB transaction_id.

    Why: §2.5 envelope; AD-5 one request = one transaction_id.
    Args:
        None
    Returns:
        str: 25 uppercase hex characters.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return uuid.uuid4().hex[:25].upper()


def build_headers(endpoint: str, idempotency_key: str | None) -> dict:
    """
    Build ESB request headers (api_key, x-signature, T-Status, Idempotency-Key).

    Why: §2.5 header convention (dummy values).
    Args:
        endpoint (str): logical endpoint name, e.g. "profile", "purchase".
        idempotency_key (str | None): set for purchase and ticket.
    Returns:
        dict: HTTP headers.
    Raises / Errors:
        None
    Side effects:
        None
    """
    api_key = rule("esb", "api_key")
    timestamp = str(int(time.time()))
    signature = hashlib.md5((api_key + rule("esb", "secret") + timestamp).encode()).hexdigest()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               rule("esb", "api_key_header"): api_key, "x-signature": signature, "x-timestamp": timestamp}
    if endpoint == "profile":
        headers["T-Status"] = "payload=secure;enc=name"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def circuit_breaker_allow(session, endpoint: str) -> bool:
    """
    Stub for the per-endpoint circuit breaker: always allows the call.

    Why: §2.7 circuit breaker is DEFERRED (DEF-01); the hook sits at the real call site.
    Args:
        session (Session): for the trace line.
        endpoint (str): logical endpoint name.
    Returns:
        bool: always True.
    Raises / Errors:
        None
    Side effects:
        Writes one "deferred" trace event.
    """
    # TODO[DEFERRED-DEF-01]: Circuit breaker per endpoint
    # Why deferred: not demonstrable in a 3-min demo; retry limits already bound failures
    # Call here when implemented: circuit_breaker.allow(endpoint: str) -> bool  (5 fails/30 s -> OPEN 60 s)
    # D2 reference: §2.7 circuit breaker
    trace.log(session, "deferred", f"deferred: circuit_breaker skipped ({endpoint})")
    return True


def is_transient(http_status: int | None, status_code: str | None) -> bool:
    """
    Decide whether a failure may be retried.

    Why: §2.7 — retry ONLY timeout, 502, 500/40000, 503/10001; never 20xxx/30xxx business errors.
    Args:
        http_status (int | None): None means timeout / connection error.
        status_code (str | None): ESB status code from the envelope.
    Returns:
        bool: True if the call should be retried.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if http_status is None:
        return True                       # timeout or connection error
    if http_status == 502:
        return True                       # provider system error
    if http_status == 500 and status_code == "40000":
        return True                       # ESB internal error
    if http_status == 503:
        return True                       # provider unreachable (10001)
    return False


def backoff_seconds(endpoint: str, attempt: int) -> float:
    """
    Wait time before retry number `attempt` (1-based).

    Why: §2.7 — reads: exponential backoff + ±30% jitter; writes: fixed 1 s, 2 s.
    Args:
        endpoint (str): logical endpoint name.
        attempt (int): retry number, 1 or 2.
    Returns:
        float: seconds to sleep.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if endpoint in WRITE_ENDPOINTS:
        return rule("esb", "retry", "write_backoff_s")[attempt - 1]
    base = rule("esb", "retry", "read_backoff_s")[attempt - 1]
    jitter = rule("esb", "retry", "jitter_ratio")
    return base * random.uniform(1 - jitter, 1 + jitter)   # jitter avoids synchronized retries


def send_once(method: str, url: str, headers: dict, params: dict | None, body: dict | None, timeout: float):
    """
    Send one HTTP request and normalize the outcome.

    Why: keeps the retry loop readable; timeouts become http_status=None.
    Args:
        method (str): "GET" or "POST".
        url (str): full ESB URL.
        headers (dict): request headers.
        params (dict | None): query parameters.
        body (dict | None): JSON body (already enveloped).
        timeout (float): client timeout in seconds.
    Returns:
        tuple: (http_status | None, response_json dict, error_text | None)
    Raises / Errors:
        None (network errors are returned as error_text).
    Side effects:
        One HTTP call to the ESB.
    """
    try:
        response = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    except requests.Timeout:
        return None, {}, "timeout"
    except requests.ConnectionError:
        return None, {}, "connection_error"
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload, None


def call_esb(session, endpoint: str, method: str, path: str, params: dict | None = None,
             payload: dict | None = None, idempotency_key: str | None = None,
             transaction_id: str | None = None, on_retry=None) -> dict:
    """
    Call one ESB endpoint with envelope, headers, timeout, retry and status mapping.

    Why: AD-6 ESB adapter; §2.7 retry rules; AD-5 same transaction_id + Idempotency-Key on retries.
    Args:
        session (Session): conversation (MSISDN for the envelope; trace).
        endpoint (str): logical name: profile, quota, offers, purchase, loan, network, ticket.
        method (str): "GET" or "POST".
        path (str): path under esb.base_url, e.g. "/subscriber/62.../quota".
        params (dict | None): query parameters (GET).
        payload (dict | None): business payload for POST (envelope is added here).
        idempotency_key (str | None): required for purchase and ticket.
        transaction_id (str | None): reuse for writes; generated if None.
        on_retry (callable | None): called once before the first retry (e.g. state -> UNKNOWN).
    Returns:
        dict: {ok, http_status, status_code, status_desc, data, transient, attempts, error}
    Raises / Errors:
        None (all failures are returned in the dict).
    Side effects:
        HTTP calls; "esb_call" / "esb_retry" trace events.
    """
    transaction_id = transaction_id or new_transaction_id()   # same ID on every retry
    url = rule("esb", "base_url") + path
    headers = build_headers(endpoint, idempotency_key)
    body = None
    if method == "POST":
        body = {"transaction": {"transaction_id": transaction_id, "channel": rule("pilot", "channel")},
                "service": {"service_id": session.msisdn}, **(payload or {})}
    max_retries = rule("esb", "retry", "write_max_retries" if endpoint in WRITE_ENDPOINTS else "read_max_retries")
    circuit_breaker_allow(session, endpoint)
    result = {}
    for attempt in range(1, max_retries + 2):          # 1 original + max_retries retries
        started = time.time()
        http_status, response_json, error = send_once(method, url, headers, params, body,
                                                      rule("esb", "timeouts_s", endpoint))
        result = summarize_response(http_status, response_json, error, attempt)
        trace.log(session, "esb_call", f"{method} {mask_text(path)} -> {http_status or error} "
                  f"{result['status_code'] or ''} ({int((time.time() - started) * 1000)} ms, attempt {attempt})",
                  endpoint=endpoint, transaction_id=transaction_id, idempotency_key=idempotency_key,
                  status_desc=result["status_desc"])
        if result["ok"] or not result["transient"] or attempt > max_retries:
            return result
        if attempt == 1 and on_retry:
            on_retry()
        wait = backoff_seconds(endpoint, attempt)
        trace.log(session, "esb_retry", f"Retry {attempt}/{max_retries} for {endpoint} in {wait:.2f}s "
                  f"with SAME transaction_id" + (" and SAME Idempotency-Key" if idempotency_key else ""),
                  idempotency_key=idempotency_key, transaction_id=transaction_id)
        time.sleep(wait)
    return result


def summarize_response(http_status, response_json: dict, error, attempt: int) -> dict:
    """
    Turn one raw HTTP outcome into the adapter's standard result dict.

    Why: callers never see raw ESB envelopes (AD-6).
    Args:
        http_status (int | None): HTTP status or None on timeout.
        response_json (dict): parsed body.
        error (str | None): "timeout" / "connection_error".
        attempt (int): attempt number.
    Returns:
        dict: {ok, http_status, status_code, status_desc, data, transient, attempts, error}
    Raises / Errors:
        None
    Side effects:
        None
    """
    envelope = response_json.get("transaction") or {}
    status_code = envelope.get("status_code")
    data = {key: value for key, value in response_json.items() if key != "transaction"}
    return {
        "ok": http_status == 200 and status_code == "00000",
        "http_status": http_status,
        "status_code": status_code,
        "status_desc": STATUS_MEANING.get(status_code, error or "unmapped"),
        "data": data,
        "transient": is_transient(http_status, status_code),
        "attempts": attempt,
        "error": error,
    }
