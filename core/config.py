"""
File: core/config.py
Purpose: Load business rules from config.toml and LLM/provider settings from .env
         into one SETTINGS dict, plus a small rule() helper to read nested values.
D2 component: Config (AD-8 config-driven, §2.11 configurable LLM)
Depends on: tomllib (stdlib), python-dotenv
"""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.toml"


def _read_llm_env() -> dict:
    """
    Read every LLM/provider setting from environment variables.

    Why: §2.11 — the LLM must be swappable by editing .env only.
    Args:
        None
    Returns:
        dict: provider, model, base_url, api_key, api_version, reasoning_effort,
              billing, temperature, timeout_s, max_tool_iterations, prices and FX.
    Raises / Errors:
        ValueError: if a numeric setting is not a number.
    Side effects:
        None
    """
    return {
        "provider": os.getenv("LLM_PROVIDER", "mock").strip(),
        "model": os.getenv("LLM_MODEL", "mock-scripted").strip(),
        "base_url": os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "api_version": os.getenv("LLM_API_VERSION", "").strip(),
        "reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "").strip(),
        "billing": os.getenv("LLM_BILLING", "free").strip(),  # free => actual cost Rp 0
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.2")),
        "timeout_s": float(os.getenv("LLM_TIMEOUT_S", "20")),
        "max_tool_iterations": int(os.getenv("LLM_MAX_TOOL_ITERATIONS", "6")),
        "price_input_usd_per_1m": float(os.getenv("PRICE_INPUT_USD_PER_1M", "0.15")),
        "price_output_usd_per_1m": float(os.getenv("PRICE_OUTPUT_USD_PER_1M", "0.60")),
        "fx_idr_per_usd": float(os.getenv("FX_IDR_PER_USD", "18000")),
    }


def load_settings() -> dict:
    """
    Build the SETTINGS dict from .env and config.toml.

    Why: AD-8 — one place for every rule, price, threshold and limit.
    Args:
        None
    Returns:
        dict: {"rules": <config.toml as dict>, "llm": <LLM env>, "app_port": int, "esb_port": int}
    Raises / Errors:
        FileNotFoundError: if config.toml is missing.
        tomllib.TOMLDecodeError: if config.toml has a syntax error.
    Side effects:
        Loads .env into os.environ (existing variables are NOT overridden, so tests can set them first).
    """
    load_dotenv(ROOT_DIR / ".env", override=False)
    with open(CONFIG_PATH, "rb") as config_file:
        rules = tomllib.load(config_file)
    return {
        "rules": rules,
        "llm": _read_llm_env(),
        "app_port": int(os.getenv("APP_PORT", "8000")),
        "esb_port": int(os.getenv("ESB_PORT", "8001")),
    }


SETTINGS = load_settings()


def rule(*path: str):
    """
    Read one nested value from config.toml, e.g. rule("cost", "cap_idr").

    Why: keeps call sites short and makes every rule lookup visibly config-driven (no magic numbers).
    Args:
        *path (str): section and key names, outermost first.
    Returns:
        Any: the configured value.
    Raises / Errors:
        KeyError: if the path does not exist in config.toml (fail fast on typos).
    Side effects:
        None
    """
    value = SETTINGS["rules"]
    for key in path:
        value = value[key]
    return value
