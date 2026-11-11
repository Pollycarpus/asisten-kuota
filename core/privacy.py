"""
File: core/privacy.py
Purpose: PII masking for the LLM context and the trace, pseudonyms (CUST_x) and
         hashed MSISDN for the transaction store.
D2 component: PII masker (§2.2, §2.8 UU PDP)
Depends on: core/config.py
"""

import hashlib
import itertools
import re

from core.config import rule

# Order matters: NIK (16 digits) is masked before phone numbers.
NIK_PATTERN = re.compile(r"\b\d{16}\b")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_PATTERN = re.compile(r"(?:\+?62|\b0)8\d{7,12}\b")
CELL_PATTERN = re.compile(r"\bCELL-[A-Z]{3}-\d{4}\b")

SENSITIVE_KEYS = {"msisdn", "service_id"}       # values are MSISDNs
CELL_KEYS = {"cell_id", "cell", "device_cell_id"}
NAME_KEYS = {"name", "display_name"}

_pseudonym_counter = itertools.count(1)


def mask_msisdn(msisdn: str) -> str:
    """
    Mask an MSISDN as 6281******01 (first 4 and last 2 digits visible).

    Why: §2.8 — MSISDN masked in logs and trace.
    Args:
        msisdn (str): e.g. "6281200000001".
    Returns:
        str: masked MSISDN, or "******" for short input.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if not msisdn or len(msisdn) < 7:
        return "******"
    return f"{msisdn[:4]}******{msisdn[-2:]}"


def mask_cell(cell_id: str) -> str:
    """
    Blur a cell ID so the precise location is not exposed (CELL-JKT-0101 -> CELL-JKT-****).

    Why: §2.8 — location blurred in logs; the LLM only sees "area Anda".
    Args:
        cell_id (str): full cell ID.
    Returns:
        str: blurred cell ID.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if not cell_id:
        return cell_id
    return cell_id[:-4] + "****" if len(cell_id) > 4 else "****"


def mask_text(text: str) -> str:
    """
    Redact NIK, email, phone/MSISDN and cell IDs from free text.

    Why: §2.8 — scrubbed chat text before the LLM and before the trace.
    Args:
        text (str): any free text (customer message, ESB path, summary).
    Returns:
        str: the text with sensitive patterns replaced.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if not isinstance(text, str):
        return text
    text = NIK_PATTERN.sub("[NIK]", text)
    text = EMAIL_PATTERN.sub("[EMAIL]", text)
    text = PHONE_PATTERN.sub(lambda match: mask_msisdn(re.sub(r"\D", "", match.group())), text)
    text = CELL_PATTERN.sub(lambda match: mask_cell(match.group()), text)
    return text


def mask_data(value, key: str = ""):
    """
    Recursively mask a dict/list/str before it is written to the trace.

    Why: §2.8 — every trace event is masked; key names decide special handling.
    Args:
        value (Any): dict, list, str or scalar.
        key (str): the dict key this value sits under (used for key-based rules).
    Returns:
        Any: a masked copy of the value.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if isinstance(value, dict):
        return {k: mask_data(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_data(item, key) for item in value]
    if not isinstance(value, str):
        return value
    if key in SENSITIVE_KEYS:
        return mask_msisdn(value)
    if key in CELL_KEYS:
        return mask_cell(value)
    if key in NAME_KEYS:
        return "***"
    return mask_text(value)


def new_pseudonym() -> str:
    """
    Create a new pseudonym like CUST_7 for one conversation.

    Why: §2.8 — the LLM receives a pseudonym only, never the MSISDN or name.
    Args:
        None
    Returns:
        str: "CUST_<n>".
    Raises / Errors:
        None
    Side effects:
        Advances the in-memory counter.
    """
    return f"CUST_{next(_pseudonym_counter)}"


def hash_msisdn(msisdn: str) -> str:
    """
    Hash an MSISDN (salted sha256) for the transaction store.

    Why: §2.8 — transaction store keeps a hashed MSISDN, TTL 24 h (purge = DEF-03).
    Args:
        msisdn (str): raw MSISDN.
    Returns:
        str: first 16 hex chars of the salted hash.
    Raises / Errors:
        None
    Side effects:
        None
    """
    salted = (rule("privacy", "msisdn_hash_salt") + msisdn).encode()
    return hashlib.sha256(salted).hexdigest()[:16]
