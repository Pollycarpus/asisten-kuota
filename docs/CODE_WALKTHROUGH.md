# Code Walkthrough

Plain-language tour of the code, so you can explain any file in about 5 minutes. About 3,400 lines of Python (roughly half are the standard docstrings and comments) + 360 lines of JS.

## 1. The main request flow (P1, happy path)

1. **Login.** The browser calls `POST /api/session {persona_id: "P1"}` (`api/routes.py → start_session`).
   - The server looks up P1's MSISDN and device cell in `data/personas_login.json` and creates a `Session` (`core/session.py`). **The MSISDN stays on the server.**
   - It calls the ESB profile endpoint through the adapter and checks consent (`guardrails.consent_status`). Granted → greeting.
2. **Customer types "Kuota saya habis".** `POST /api/chat` → `core/agent.py → run_turn`:
   1. The text is masked (`privacy.mask_text`) and logged.
   2. `screen_turn` applies deterministic guardrails (human request / injection / anger) **before** any LLM call.
   3. `run_tool_loop` calls the LLM (`llm_client.chat`) with the system prompt + masked history + tool schemas, at most `LLM_MAX_TOOL_ITERATIONS` times. Before every call the cost guard runs.
3. **The LLM asks for tools.** `tools.execute` runs each one:
   - `get_quota` and `get_network_status` call the ESB via `esb_adapter.call_esb`. When both are known, `diagnosis.diagnose` (rules, not LLM) returns `QUOTA_EXHAUSTED`. The UI gets a diagnosis card.
   - `get_offers` calls the ESB, then `diagnosis.rank_offers` keeps the **Top-3** (the whitelist) with an `affordable` flag. The UI gets package cards.
   - The LLM recommends one package in Bahasa Indonesia.
4. **Customer taps "Pilih paket ini".** That just sends a chat message. The LLM calls `purchase(offer_id)` → `tools.tool_purchase` checks the whitelist, the lock and pulsa, then **does not buy**. It calls `transactions.create_confirmation`, and the UI shows **[Konfirmasi] [Batal]**.
5. **Customer taps Konfirmasi.** `POST /api/confirm` (`api/routes.py → confirm`) is the **only** code path that buys:
   1. `transactions.consume_confirmation` checks the confirmation is not used or expired and marks it used.
   2. `execute_purchase` creates one `transaction_id` + one `Idempotency-Key`, moves the state machine, and calls `POST /purchase` through the adapter.
   3. On success it verifies with `get_quota` and replies "Paket … sudah aktif".

## 2. File by file

| File | What it does | Read this first |
|---|---|---|
| `app.py` | Starts two threaded servers: app (UI + API) on 8000, mock ESB on 8001. | `main()` |
| `config.toml` | Every business rule. See `docs/CONFIG_GUIDE.md`. | — |
| `core/config.py` | Loads `config.toml` + `.env` into `SETTINGS`; `rule("cost","cap_idr")` reads one value. | `rule()` |
| `core/session.py` | `Session` dataclass: everything about one conversation (MSISDN, pseudonym, consent, history, whitelist, confirmations, transactions, trace, cost). | `Session` |
| `core/privacy.py` | Masking (MSISDN, cell, NIK, email, phone), `CUST_n` pseudonyms, salted MSISDN hash. | `mask_data()` |
| `core/trace.py` | `log()` writes one masked event; `add_llm_usage()` updates the cost meter. | `add_llm_usage()` |
| `core/esb_adapter.py` | The only door to the ESB: envelope, headers, timeout, retry rules, same key on write retries, status mapping. | `call_esb()`, `is_transient()` |
| `core/diagnosis.py` | Rules engine (4 statuses) and Top-N ranking. | `diagnose()` |
| `core/guardrails.py` | Consent, message screening, whitelist, Paket Darurat policy, cost status, handoff with masked summary. | `screen_message()`, `do_handoff()` |
| `core/transactions.py` | Confirmation gate + state machine + purchase lock + ticket dedup. | `consume_confirmation()`, `execute_purchase()` |
| `core/tools.py` | Tool schemas for the LLM and their executors. The LLM fills only business choices; the system fills MSISDN, cell, prices and keys. | `tool_purchase()` |
| `core/llm_client.py` | `chat(messages, tools)` for `azure_openai`, `openai_compatible` (Gemini) and `mock`. Handles 429 (wait once), malformed tool JSON (retry once), missing usage (estimate). | `chat()` |
| `core/mock_llm.py` | Scripted "LLM": reads the conversation and returns the same message/tool-call shape as a real provider. | `respond()` |
| `core/agent.py` | System prompt, history trimming, guardrail screen, tool loop, fallbacks. | `run_turn()` |
| `api/routes.py` | Our API: personas, session, consent, chat, confirm, cancel, CSAT, handoff, trace, reset. Purchase/ticket outcome messages are fixed templates (no LLM). | `confirm()` |
| `mock_esb/server.py` | The 7 brief endpoints + idempotency store + ledger + faults + loan rules. | `purchase()`, `check_idempotency()` |
| `static/app.js` | UI: renders bubbles and cards, buttons call `/api/confirm`, refreshes trace and cost meter. | `confirmationCard()` |
| `tests/` | Scenario tests; the mock ESB runs on a free port over real HTTP. | `test_personas.py` |

## 3. Key design decisions, in one line each

- **Why the LLM can't buy:** `purchase` only creates a confirmation; buying lives in `/api/confirm` (AD-2, AD-4).
- **Why no double charge after a timeout:** the key is created once per confirmation and reused on every retry. The mock returns the stored result for a known key (AD-5, §2.7).
- **Why the timeout is real:** the adapter calls the mock over HTTP with a 5 s timeout, and the mock sleeps 6 s **after** storing the result.
- **Why two ports:** the ESB looks and behaves like an external system; the browser never calls it.
- **Why keyword handoff:** deterministic, testable and explainable. An LLM classifier is DEF-11.
- **Why deterministic confirm/cancel messages:** cheaper (no LLM call) and they can't hallucinate a purchase status.
- **Why the "known state" line in the system prompt:** after history trimming, the model still knows the diagnosis and the whitelist.
