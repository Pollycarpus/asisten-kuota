---
name: d4-prototype-engineer
description: Build and change the Asisten Kuota D4 prototype (Flask app, mock ESB, agent, guardrails, UI, tests, README) strictly within the approved MVP scope. Use for any code change in this repo.
---

# D4 Prototype Engineer — Asisten Kuota

## Scope is fixed
- Build only ESSENTIAL and SIMPLIFIED items from the approved Phase 2 triage. Do **not** redesign D2 or add features.
- Every DEFERRED item (DEF-01 … DEF-12) keeps its `TODO[DEFERRED-<ID>]` hook at its call site. A hook is a no-op stub that returns the "happy" value and writes one trace line `deferred: <feature> skipped`.
- Gaps found while building go to `docs/AI_ASSISTANCE_LOG.md` → "Change requests", not into code.

## Architecture (approved)
- Two ports, one command: `python app.py` → app (UI + `/api`) on `APP_PORT` (8000) and mock ESB (`/esb`) on `ESB_PORT` (8001).
- The backend reaches the ESB **only** through `core/esb_adapter.py` over real HTTP.
- The LLM only reasons and talks (AD-2). Purchases and tickets execute **only** in `core/transactions.confirm()` called from `POST /api/confirm` (button). Typed "ya" never buys.
- MSISDN comes from the session (simulated login), never from LLM output or typed text. The LLM sees `CUST_x`, numbers and "area Anda" only.

## Non-negotiable guardrails (deterministic, provider-independent)
consent check · Top-3 offer whitelist · confirmation gate (single-use, 5-min expiry) · 1 request = 1 transaction_id + 1 Idempotency-Key, retries reuse it · purchase lock · upsell suppression when network degraded · cost guard (warn/cap) · handoff rules · PII masking before LLM and trace.

## Coding standard
- Every file starts with the header (File / Purpose / D2 component / Depends on).
- Every function has the standard docstring: summary, Why, Args, Returns, Raises / Errors, Side effects.
- No magic numbers: all rules, prices, thresholds, timeouts live in `config.toml` (business) or `.env` (LLM provider).
- Comments in English; customer-facing strings in Bahasa Indonesia.
- Functions ≈ ≤ 40 lines, descriptive names, no clever one-liners, no dead code.

## Definition of done for any change
1. `python -m pytest -q` passes with `LLM_PROVIDER=mock`.
2. Header + docstrings present in touched files.
3. README / CONFIG_GUIDE / CODE_WALKTHROUGH updated if behaviour or config changed.
4. AI assistance log updated.
