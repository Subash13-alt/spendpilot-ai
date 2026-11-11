# SpendPilot AI

Intelligent enterprise expense management using **Python, Flask, spaCy, scikit-learn and SQLite**.
Six cooperating agents behind one central orchestrator extract receipts, check policy, flag anomalies, recommend
decisions, track budgets and prepare reimbursements. **Humans always approve; AI only advises.**
Runs fully locally: no external database, no paid or cloud AI API.

## Install and run (Windows)

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
python app.py
```

Open <http://127.0.0.1:5000>. On first start the app creates `spendpilot.db`, loads the NEXORA TECHNOLOGIES demo data
and runs four demo scenarios through the real agent pipeline.

**Optional OCR for images (PNG/JPG):** install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) and either add it
to `PATH` or set `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`. `.txt` and text-based `.pdf` receipts work without it.
Without Tesseract the app says so and lets the employee type the values.

**PayPal Sandbox (optional):**
```bat
set PAYPAL_CLIENT_ID=your-sandbox-client-id
set PAYPAL_CLIENT_SECRET=your-sandbox-secret
python app.py
```
Credentials are read from the environment only and never reach templates or JavaScript.
Set `SPENDPILOT_SECRET` to a long random string for anything beyond a local demo.

## Demo accounts (password `Password123!`)

| Role | Email |
|---|---|
| Employee (Engineering) | alice@nexora.com, bob@nexora.com |
| Employee (Marketing) | carol@nexora.com |
| Department Manager | manager.eng@nexora.com, manager.mkt@nexora.com, manager.ops@nexora.com, manager.hr@nexora.com |
| Finance Admin | finance@nexora.com |
| Auditor | auditor@nexora.com |
| Super Admin | admin@nexora.com |

## Demonstration walk-through

1. Sign in as **alice@nexora.com** > *Submit expense* > upload `sample_receipts/hotel_grand_palace.png` (OCR) or `.txt`.
   The fields fill automatically: Hotel Grand Palace, 2026-11-09, $350.00, USD, Accommodation. Submit.
2. The expense detail page shows the **$200 limit, $150 excess**, duplicate check, anomaly risk score, and a written AI explanation.
3. Sign in as **manager.eng@nexora.com** > *Approvals* > approve or reject.
4. Sign in as **finance@nexora.com** > *Approvals* > authorize payment > *Payments* > *Send payout*.
5. Sign in as **auditor@nexora.com** > *Audit log* to see every status change and agent run.

Pre-loaded scenarios (Engineering manager queue, Marketing budget page, AI Intelligence page):
- **Policy violation**: Alice's $350 hotel receipt.
- **Duplicate receipt**: the same file submitted twice -> HIGH risk, FLAGGED, manager must comment to proceed.
- **Suspicious spending**: Bob's $480 steakhouse dinner vs. a ~$25 typical meal -> MEDIUM risk alert.
- **Normal expense**: Carol's $24.50 lunch -> LOW risk.
- **Budget warning**: Marketing sits above 80% of its budget (*Budgets* page).
- **Natural-language query**: *Analytics* > "What is the total engineering expenditure?", "Show all expenses above $500", etc.

## Architecture

```
Employee upload
  -> Expense Intelligence Agent   OCR + spaCy EntityRuler + regex + classifier
  -> Policy Compliance Agent      deterministic Decimal maths on policies
  -> Fraud Detection Agent        file hash, duplicate tx, robust z-score, Isolation Forest
  -> Financial Intelligence Agent budget impact
  -> Approval Intelligence Agent  routing + recommendation (advice only)
  -> Central decision engine      PENDING_MANAGER_APPROVAL or FLAGGED (never an approval)
  -> Human: manager -> finance administrator
  -> Payment Agent                PayPal Sandbox payout, verified server-side
  -> SQLite + audit trail -> dashboards
```

| Folder | Purpose |
|---|---|
| `database/` | sqlite3 connection + SAVEPOINT transactions, schema (12 tables, FKs, indexes), seed data |
| `agents/` | the six agents, `base.py` (shared `WorkflowState`), `orchestrator.py` |
| `intelligence/` | spaCy engine, entity extractor, classifier, policy engine, anomaly detector, forecasting |
| `services/` | receipt validation/OCR, approval rules, PayPal client, audit + legal status transitions, row scoping |
| `routes/` | Flask blueprints (auth, expenses, approvals, payments, analytics, agents/audit, admin) |
| `templates/`, `static/` | Bootstrap UI, Chart.js, Lucide icons (vendored under `static/vendor`, so it works offline) |
| `tests/` | 80+ pytest tests: `python -m pytest tests -q` |

## Safety rules enforced in code (and tested)

- **No AI authorisation.** `MANAGER_APPROVED` and `FINANCE_AUTHORIZED` can only be reached by a human actor; `audit_service.transition` rejects agents.
- **Legal transitions only**, each written to `audit_logs`. `PAID` is set only when PayPal's API returns the payout item as `SUCCESS`.
- **Agent failure is safe**: it routes the expense to `FLAGGED` for human review, never to approval.
- **Separation of duties**: nobody approves their own expense; managers act only on their department; only finance authorises/pays.
- **Organisation isolation** and role scoping on every query (`services/scope.py`); roles enforced by backend decorators, not just hidden menu items.
- **Natural-language queries never run user SQL**: spaCy maps a question to a fixed set of parameterised queries.
- Passwords hashed (werkzeug scrypt), CSRF tokens on every POST, login lockout, uploads validated by extension, size and magic bytes and stored under random names, output escaped.
- Fraud output is a **risk indicator** with a disclaimer; it never says anyone is guilty.

## Honest limitations

- **PayPal:** the Sandbox integration follows PayPal's Payouts API (OAuth token, create payout, server-side status lookup) and is unit-tested
  against a scripted fake HTTP session. It was **not run against live PayPal servers** in the build environment, which had no credentials.
  Try it with your own sandbox keys. Sandbox payout items often stay `PENDING`/`UNCLAIMED` for a while; use *Refresh status*.
  Payouts must be enabled for your sandbox business account, and the employee's PayPal email must be a sandbox account.
- **Demo mode:** without credentials, *Send payout* records a clearly labelled `DEMO_NOT_PAID` reimbursement. No money moves and the expense is never marked paid.
- **Seeded history:** historical baseline expenses are inserted as `MANAGER_APPROVED` (audit action `SEEDED_DEMO_DATA`), never `PAID`.
- **Numeric dates** such as `09/11/2026` are read as DD/MM/YYYY (matching the specification example) and the employee is asked to confirm.
- **Currencies:** policy limits are in USD. Other currencies are routed to manager review because no exchange-rate source is configured.
- **Dev server:** `python app.py` uses Flask's built-in server, fine for a demo. For production use a WSGI server (e.g. `waitress`) behind HTTPS and set `SPENDPILOT_SECRET`.
- The login lockout counter is in process memory and resets on restart.
