# Config Guide — how to change rules without touching code

All business rules live in **`config.toml`** (the project root). LLM provider settings and secrets live in **`.env`**.
Config is read **once at start-up**, so after every edit: **stop the app (Ctrl+C) and run `python app.py` again.**

| I want to change… | File | Section |
|---|---|---|
| Paket Darurat rules (fee, limit, who is eligible, on/off) | `config.toml` | `[paket_darurat]` and `[mock_esb.loan_rules]` — see §1 |
| Cost warning / cap per conversation | `config.toml` | `[cost]` |
| Token prices or exchange rate for the projected cost | `.env` | `PRICE_INPUT_USD_PER_1M`, `PRICE_OUTPUT_USD_PER_1M`, `FX_IDR_PER_USD` |
| When the customer is handed to a human | `config.toml` | `[handoff]` |
| What counts as "quota exhausted", how many offers are shown, which offer types fit which problem | `config.toml` | `[diagnosis]` |
| ESB address, timeouts, retries | `config.toml` | `[esb]`, `[esb.timeouts_s]`, `[esb.retry]` |
| Confirmation expiry (5 min) | `config.toml` | `[agent] confirmation_ttl_s` |
| Pilot region / channel | `config.toml` | `[pilot]` |
| Which LLM is used | `.env` | `LLM_*` (see README §3) |

---

## 1. Paket Darurat (emergency package / loan)

Paket Darurat rules are split in two blocks because **two different systems own them** in real life:

| Block | Who owns it in production | What it decides |
|---|---|---|
| `[paket_darurat]` | **Asisten Kuota** (our agent policy) | *Whether the agent is allowed to offer* a loan at all, and for which packages |
| `[mock_esb.loan_rules]` | **The loan/credit system behind the ESB** (simulated by the mock) | *Whether this customer is eligible*, the limit and the fee |

The flow is: pulsa too low → **agent policy check** (`[paket_darurat]`) → if allowed, **ESB eligibility check** (`[mock_esb.loan_rules]`) → if eligible, a confirmation card with **price + fee + total + repayment** → customer taps **Konfirmasi** → purchase with `payment_method = PAKET_DARURAT`.

### 1.1 Agent policy — `[paket_darurat]`

| Setting | Default | Allowed values | What it does |
|---|---|---|---|
| `enabled` | `true` | `true` / `false` | `false` = the agent never offers Paket Darurat; it suggests top-up or a cheaper package instead. |
| `offer_when_pulsa_below_price` | `true` | `true` / `false` | `true` = only offer a loan when the package price is higher than the current pulsa. `false` = also allowed when pulsa is enough (not recommended). |
| `max_offer_price_idr` | `25000` | whole number (Rupiah) | The agent never offers a loan for a package more expensive than this, even if the provider would allow it. |
| `allowed_categories` | `["internet", "darurat"]` | list of offer categories (`internet`, `booster`, `darurat`) | Only these package types can be bought with a loan. |
| `max_loans_per_conversation` | `1` | whole number | How many Paket Darurat purchases one conversation may make. |
| `topup_link` | dummy link | URL | Link shown when the customer is not eligible. |

### 1.2 Provider eligibility rules — `[mock_esb.loan_rules]`

A customer is **eligible only if all rules pass**. Customer inputs come from `data/esb_customers.json` → `loan_profile`.

| Setting | Default | Allowed values | Rule |
|---|---|---|---|
| `min_tenure_days` | `90` | whole number | Customer must have been active at least this many days. |
| `min_avg_monthly_topup_idr` | `20000` | whole number | Average monthly top-up must be at least this. |
| `block_if_outstanding_loan` | `true` | `true` / `false` | An unpaid Paket Darurat blocks a new one. |
| `limit_idr` | `20000` | whole number | Maximum loan amount; the package price must be ≤ this. |
| `fee_type` | `"flat"` | `"flat"` / `"percent"` | How the fee is calculated. |
| `fee_flat_idr` | `2500` | whole number | Fee when `fee_type = "flat"`. |
| `fee_percent` | `10` | number | Fee = price × percent / 100 (rounded) when `fee_type = "percent"`. |
| `repayment_text` | "Dipotong otomatis saat isi ulang pulsa berikutnya" | text (Bahasa Indonesia) | Shown on the confirmation card. |

**Fixed in code on purpose (not configurable):** the fee is always shown on the confirmation card; a loan purchase always needs the button tap; the agent never buys automatically. These are transparency / UU PDP guardrails, not business settings.

### 1.3 Example edits

**A. Fee changes from flat Rp 2.500 to 10%**
```toml
[mock_esb.loan_rules]
fee_type = "percent"
fee_percent = 10
```
Result for P3 (Internet 1GB, Rp 8.000): fee Rp 800, total Rp 8.800.

**B. Loan limit drops from Rp 20.000 to Rp 15.000**
```toml
[mock_esb.loan_rules]
limit_idr = 15000
```
Result: packages above Rp 15.000 are no longer eligible. P3's cheapest package (Rp 8.000) still is.

**C. Stricter eligibility: 180 days tenure**
```toml
[mock_esb.loan_rules]
min_tenure_days = 180
```
Result: P3 (tenure 400 days) still eligible; P5 (30 days) still not.

**D. Switch Paket Darurat off for the pilot**
```toml
[paket_darurat]
enabled = false
```
Result: P3 gets a top-up suggestion instead of a loan offer; no ESB eligibility call is made (visible in the trace).

**E. Agent may only lend for small packages**
```toml
[paket_darurat]
max_offer_price_idr = 10000
```

### 1.4 How to verify a change
1. Save `config.toml`, restart: `Ctrl+C`, then `python app.py`.
2. Open http://localhost:8000 → **P3** → type `Kuota saya habis`. Check the confirmation card (price, fee, total) and the trace lines `Paket Darurat policy: …` and `POST /loan/eligibility`.
3. **P5** → accept consent → `Kuota saya habis` → the affordable package is recommended (P5 is not eligible for a loan).
4. Run the tests: `python -m pytest -q tests/test_paket_darurat.py`. Fee assertions **read the config**, so they follow your change.
5. If you change a rule a test hard-codes (e.g. `test_limit_below_price_makes_customer_not_eligible` sets the limit to Rp 5.000 itself), the test still passes because it sets its own value. Persona scenario tests (`tests/test_personas.py`) assume P3 stays eligible; if your change makes P3 ineligible, update `test_p3_paket_darurat_shows_fee_and_pays_by_loan` or the P3 data in `data/esb_customers.json`.

### 1.5 Template for the README "Dampak update constraint" section
```markdown
## Dampak update constraint (H+2)
**What changed:** <e.g. Paket Darurat fee changes from flat Rp 2.500 to 10% of the package price>
**Why:** <business reason from the update>
**Where it was changed:** config.toml → [mock_esb.loan_rules] fee_type, fee_percent (no code change)
**Effect on the customer:** <e.g. P3 fee Rp 2.500 → Rp 800; the card still shows price, fee and total>
**Effect on deliverables:** D3 edge case (a) sample conversation updated; D4 tests green (fees read from config)
**Trade-off:** <e.g. lower fee → higher loan uptake but lower fee revenue / higher credit risk>
**Verified by:** pytest (21 passed) + manual P3 run
```

---

## 2. Other common changes

**Cost cap** (`[cost]`): `warn_idr` writes a trace warning; `cap_idr` hands the customer to a human before the next LLM call. To demo the cap quickly, set `cap_idr = 20` and run any persona.

**Handoff** (`[handoff]`): `human_keywords` and `anger_keywords` are matched as whole words, case-insensitive. `deescalations_before_handoff = 1` means: first angry message → apology; second → handoff. `ticket_failures_before_handoff = 2`.

**Diagnosis** (`[diagnosis]`): `quota_exhausted_mb` (default 100 MB) is the threshold for QUOTA_EXHAUSTED. `[diagnosis.preferred_categories]` decides which offer types rank first per diagnosis; `NETWORK_DEGRADED = []` keeps upsell suppressed.

**ESB** (`[esb]`): `base_url` points at the mock (`http://localhost:8001/esb`); a real ESB is a one-line change. `api_key_header` is `api-key` for the mock (the dev server drops header names with underscores); a real ESB uses `api_key`.
