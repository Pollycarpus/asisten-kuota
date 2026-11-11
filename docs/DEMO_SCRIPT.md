# Demo Video Script (≤ 3:00)

**Setup before recording:** `python app.py` running; browser at http://localhost:8000, full screen, zoom about 90%. Press **Reset demo**. Decide the LLM: Gemini (`.env`) for the real agent, or `mock` if the Wi-Fi or quota is unreliable. Say which one in the video: it is shown at the top of the trace panel. Rehearse once.

| Time | Screen | Do / type | Say (key points) |
|---|---|---|---|
| 0:00–0:15 | App open | — | "Customers whose quota runs out don't know which package fits, and slow-internet complaints become false network tickets. Asisten Kuota diagnoses, recommends, and acts, but only with explicit consent." |
| 0:15–0:55 | **P1** → Mulai sesi | `Kuota saya habis` | Diagnosis card = **rules engine**, not the LLM. Trace: LLM tool calls `get_quota`, `get_network_status`, `get_offers` → ESB calls with masked MSISDN `6281******01`. |
| | | **Pilih paket ini** → type `ya` | "Typing 'ya' buys nothing." |
| | | **Konfirmasi** | Trace: `AWAITING_CONFIRMATION → CONFIRMED → SUBMITTING → SUCCEEDED`, one Idempotency-Key. Quota verified after purchase. |
| 0:55–1:25 | **P3** → Mulai sesi | `Kuota habis` | Pulsa Rp 2.000 → Paket Darurat. Point at **fee Rp 2.500 and total** on the card ("never hide the fee"). Rules come from `config.toml`. **Konfirmasi**. |
| 1:25–2:05 | **P4** → Mulai sesi | `Sinyal jelek, internet putus-putus` | Quota and FUP normal → NETWORK_DEGRADED → **no package offered** (upsell suppressed). **Buat laporan** → ticket number. |
| | | `Payah banget!!!` | One apology (de-escalation). |
| | | `Kalian tidak becus` | Handoff: ref ID + **masked summary** the human receives. |
| 2:05–2:45 | **P5** → Mulai sesi | **Setuju** (consent card) → `Kuota habis` → **Pilih paket ini** (Internet 2GB) → **Konfirmasi** (≈ 6 s wait) | "No consent, no AI." In the trace: `POST /purchase → timeout` → `UNKNOWN` → `Retry … SAME Idempotency-Key` → `idempotent replay` → `SUCCEEDED`. "One charge, never two." |
| 2:45–3:00 | Cost meter | Point at it | "Projected production cost about Rp 20 of the Rp 250 cap, computed at GPT-4o-mini prices even on the free tier. Every rule is in one config file, ready for the H+2 change." |

**Backup lines if something fails live:** switch `.env` to `LLM_PROVIDER=mock`, restart, and continue. The guardrails are identical.
