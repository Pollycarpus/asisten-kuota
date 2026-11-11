---
name: qa-reviewer
description: Verify the Asisten Kuota prototype before submission — run the scenario tests, smoke-test the live app, and walk the final checklist. Use before any demo, recording or submission.
---

# QA Reviewer — Asisten Kuota

## Steps
1. `python -m pytest -q` with `LLM_PROVIDER=mock` → all green.
2. Start `python app.py`; open http://localhost:8000; run P1 → P5 by hand using `docs/DEMO_SCRIPT.md`.
3. In the trace panel confirm: every tool call, ESB call, retry, state transition and guardrail decision is visible; no unmasked MSISDN, name or full cell_id anywhere.
4. P5: the trace shows a timeout, a retry **with the same Idempotency-Key**, `idempotent_replay=true`, and exactly one charge.
5. Typing "ya" while a confirmation card is open must not buy anything.
6. Grep checks:
   - `grep -rn "TODO\[DEFERRED-" core mock_esb api` → every DEF-ID in the README register appears.
   - No real-looking secrets in the repo; `.env` not committed.
7. Walk the final checklist in the master prompt §12 and report pass/fail per line.
