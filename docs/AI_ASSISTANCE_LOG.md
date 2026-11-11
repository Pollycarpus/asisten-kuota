# AI Assistance Log

Running log of what the AI (Claude, Anthropic) generated and what the author decided or changed. Required by the hackathon rules (README §12).

## Session 1 — Fri 13 Nov 2026

| Step | AI did | Author decided / changed |
|---|---|---|
| Phase 1: platform & LLM | Compared Flask / FastAPI / Express; researched free-tier LLM limits | Approved Python + Flask; approved Gemini Flash; **rejected Groq** |
| Phase 1: hosting | Listed local install prerequisites | Chose local hosting |
| Phase 2: scope triage | Classified every D2 component; proposed DEF-01…DEF-12 | Approved; **added: Paket Darurat rules must be configurable + adjustment guide** |
| Phase 3: architecture | Proposed folder tree, tools, API; explained port options | **Chose 2 ports** (app 8000, mock ESB 8001) |
| Phase 4: skills | Wrote `.claude/skills/` d4-prototype-engineer, d3-cx-designer, qa-reviewer | — |
| Phase 5: build | Generated all code, data, tests and docs; ran tests and a headless-browser UI check | *to fill after review* |

## Issues found and fixed during the build (by the AI, verified by tests)
- The Werkzeug dev server drops header names with underscores, so `api_key` never reached the mock ESB. The header name became a config value (`api-key` for the mock).
- History trimming made the model forget the diagnosis. Added a "known state" line (diagnosis, Top-3, ticket) to the system prompt.
- A keyword clash in the handoff trace call crashed the angry-customer handoff. Fixed and covered by `test_p4_…`.

## Change requests (gaps for later, not built)
- None beyond the Deferred Feature Register (README §10).

## Author review notes
*(Fill in: files you read line by line, anything you changed, questions you resolved.)*
