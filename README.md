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
