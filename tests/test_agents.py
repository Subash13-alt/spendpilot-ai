import io
import json

import pytest
from werkzeug.datastructures import FileStorage

from agents.base import WorkflowState
from agents.expense_agent import ExpenseIntelligenceAgent
from agents.fraud_agent import FraudDetectionAgent
from agents.policy_agent import PolicyComplianceAgent
from database.connection import connect
from database.schema import init_schema
from database.seed import seed_base
from intelligence.anomaly_detector import isolation_forest_score, zscore_check
from intelligence.entity_extractor import extract_date, extract_receipt_entities
from intelligence.expense_classifier import classify_expense
from intelligence.policy_engine import evaluate_expense, parse_policy_text
from services import receipt_service


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    seed_base(c)
    yield c
    c.close()


def add_expense(conn, user_email="alice@nexora.com", merchant="Hotel Grand Palace", amount=350.0,
                category="Accommodation", date="2026-11-09", receipt_hash=None, status="SUBMITTED"):
    u = conn.execute("SELECT id, org_id, department_id FROM users WHERE email = ?", (user_email,)).fetchone()
    cur = conn.execute(
        "INSERT INTO expenses (org_id, user_id, department_id, merchant, expense_date, amount, currency, category,"
        " receipt_hash, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (u["org_id"], u["id"], u["department_id"], merchant, date, amount, "USD", category, receipt_hash, status))
    return cur.lastrowid


def state_for(conn, eid):
    row = dict(conn.execute("SELECT * FROM expenses WHERE id = ?", (eid,)).fetchone())
    return WorkflowState(expense_id=eid, org_id=row["org_id"], expense=row)


# ---------- extraction ----------
def test_spec_example_extraction():
    out = extract_receipt_entities("Hotel Grand Palace. Date: 09/11/2026. Total: $350.00")
    assert out["merchant"] == "Hotel Grand Palace"
    assert out["date"] == "2026-11-09"
    assert out["amount"] == 350.00
    assert out["currency"] == "USD"


def test_total_preferred_over_subtotal_tax_change():
    text = "Cafe Roma\n12 Mar 2026\nSubtotal $10.00\nTax $0.80\nTotal $10.80\nCash $20.00\nChange $9.20"
    out = extract_receipt_entities(text)
    assert out["amount"] == 10.80 and out["date"] == "2026-03-12"


def test_unambiguous_dates():
    assert extract_date("on 25/12/2026")["value"] == "2026-12-25"
    assert extract_date("12/25/2026")["value"] == "2026-12-25"
    assert extract_date("2026-02-30")["value"] is None  # invalid calendar date


def test_currency_symbols():
    assert extract_receipt_entities("Taxi Co\nTotal €45.50")["currency"] == "EUR"
    assert extract_receipt_entities("Taxi Co\nTotal GBP 45.50")["currency"] == "GBP"


def test_empty_and_garbage_receipts_do_not_crash():
    assert extract_receipt_entities("")["amount"] is None
    out = extract_receipt_entities("@@@ ### !!!")
    assert out["amount"] is None and out["notes"]


def test_absurd_amount_rejected():
    assert extract_receipt_entities("Shop\nTotal $99,999,999.00")["amount"] is None


# ---------- classification ----------
@pytest.mark.parametrize("text,merchant,cat", [
    ("Room 4 two nights", "Hotel Grand Palace", "Accommodation"),
    ("Lunch for team", "Pizza Corner Restaurant", "Food"),
    ("Airport ride", "Uber", "Transportation"),
    ("Monthly subscription license", "GitHub", "Software"),
    ("Blackjack and poker chips", "Lucky Casino", "Gambling"),
    ("misc", "Zzz Holdings", "Other"),
])
def test_classifier(text, merchant, cat):
    assert classify_expense(text, merchant)["category"] == cat


# ---------- policy ----------
def test_policy_violation_matches_spec():
    r = evaluate_expense({"limit_amount": 200, "prohibited": 0}, "Accommodation", 350)
    assert (r["status"], r["limit"], r["actual_amount"], r["excess_amount"]) == ("VIOLATION", 200, 350, 150)
    assert r["recommendation"] == "Manager review required"


def test_policy_compliant_prohibited_and_foreign_currency():
    assert evaluate_expense({"limit_amount": 100, "prohibited": 0}, "Food", 100)["status"] == "COMPLIANT"
    assert evaluate_expense({"limit_amount": None, "prohibited": 1}, "Alcohol", 5)["status"] == "PROHIBITED"
    assert evaluate_expense({"limit_amount": 100, "prohibited": 0}, "Food", 50, "EUR")["status"] == "REVIEW"
    assert evaluate_expense(None, "Other", 5)["status"] == "NO_POLICY"


def test_float_precision_boundary():
    assert evaluate_expense({"limit_amount": 0.3, "prohibited": 0}, "Food", 0.1 + 0.2)["status"] == "COMPLIANT"


def test_parse_natural_language_policy():
    p = parse_policy_text("Hotel stays are limited to $200 per night.")
    assert p == {"category": "Accommodation", "limit_amount": 200.0, "prohibited": False}
    p = parse_policy_text("Alcohol purchases are not reimbursable.")
    assert p["category"] == "Alcohol" and p["prohibited"] is True
    with pytest.raises(ValueError):
        parse_policy_text("Be nice to everyone.")


def test_policy_agent(conn):
    state = state_for(conn, add_expense(conn))
    out = PolicyComplianceAgent().execute(conn, state)
    assert out["status"] == "VIOLATION" and out["excess_amount"] == 150


# ---------- receipt service ----------
def _fs(name, data):
    return FileStorage(stream=io.BytesIO(data), filename=name)


def test_receipt_validation(tmp_path):
    with pytest.raises(receipt_service.ReceiptError):
        receipt_service.validate_and_store(_fs("x.exe", b"MZ"), str(tmp_path))
    with pytest.raises(receipt_service.ReceiptError):
        receipt_service.validate_and_store(_fs("x.png", b"not a png"), str(tmp_path))
    with pytest.raises(receipt_service.ReceiptError):
        receipt_service.validate_and_store(_fs("x.txt", b""), str(tmp_path))
    with pytest.raises(receipt_service.ReceiptError):
        receipt_service.validate_and_store(_fs("x.txt", b"a" * 20), str(tmp_path), max_bytes=10)
    ok = receipt_service.validate_and_store(_fs("../../evil.txt", b"Hotel X Total $5.00"), str(tmp_path))
    assert ok.path.startswith(str(tmp_path)) and ".." not in ok.path and len(ok.sha256) == 64


def test_corrupted_image_handled(tmp_path):
    st = receipt_service.validate_and_store(_fs("x.png", b"\x89PNG\r\n\x1a\ngarbage"), str(tmp_path))
    res = receipt_service.extract_text(st.path, st.ext)
    assert res["text"] == "" and res["warning"]


def test_expense_agent_end_to_end_text_file(tmp_path):
    agent = ExpenseIntelligenceAgent()
    rec = agent.analyse_receipt(_fs("r.txt", b"Hotel Grand Palace. Date: 09/11/2026. Total: $350.00"),
                                str(tmp_path), 5 * 1024 * 1024)
    assert (rec["merchant"], rec["date"], rec["amount"], rec["currency"], rec["category"]) == \
           ("Hotel Grand Palace", "2026-11-09", 350.0, "USD", "Accommodation")
    assert rec["receipt_hash"]


def test_expense_agent_run_reports_corrections(conn):
    eid = add_expense(conn)
    conn.execute("UPDATE expenses SET extraction_json = ? WHERE id = ?",
                 (json.dumps({"merchant": "Hotel Grand Palace", "amount": 305.0, "raw_text": "hotel room"}), eid))
    out = ExpenseIntelligenceAgent().execute(conn, state_for(conn, eid))
    assert any(c["field"] == "amount" for c in out["employee_corrections"])
    assert out["has_receipt"] is False


# ---------- fraud ----------
def test_duplicate_receipt_hash_is_high_risk(conn):
    add_expense(conn, receipt_hash="abc123", date="2026-10-01")
    eid = add_expense(conn, receipt_hash="abc123", date="2026-10-02")
    out = FraudDetectionAgent().execute(conn, state_for(conn, eid))
    assert out["risk_level"] == "HIGH" and out["risk_score"] >= 70
    assert "Identical receipt" in out["reason"]
    assert "fraud" not in out["recommendation"].lower() and "guilty" not in out["reason"].lower()


def test_duplicate_transaction_without_hash(conn):
    add_expense(conn, merchant="Hotel  Grand-Palace", amount=350, date="2026-10-01")
    eid = add_expense(conn, merchant="hotel grand palace", amount=350, date="2026-10-01")
    out = FraudDetectionAgent().execute(conn, state_for(conn, eid))
    assert out["risk_score"] >= 70 and "duplicate transaction" in out["reason"].lower()


def test_unusual_amount_scores_above_normal(conn):
    for i in range(8):
        add_expense(conn, merchant=f"Cafe {i}", amount=20 + i, category="Food", date=f"2026-10-{i + 1:02d}")
    normal = add_expense(conn, merchant="Cafe X", amount=24, category="Food", date="2026-10-20")
    weird = add_expense(conn, merchant="Cafe Y", amount=480, category="Food", date="2026-10-21")
    n = FraudDetectionAgent().execute(conn, state_for(conn, normal))
    w = FraudDetectionAgent().execute(conn, state_for(conn, weird))
    assert w["risk_score"] > n["risk_score"] and w["risk_level"] in ("MEDIUM", "HIGH")


def test_zscore_and_isolation_forest_unit():
    assert zscore_check(500, [20, 22, 25, 21, 24, 23])["score"] == 1.0
    assert zscore_check(24, [20, 22, 25, 21, 24, 23])["score"] == 0.0
    assert zscore_check(5, [1, 2])["available"] is False
    hist = [{"amount": 20 + (i % 10), "category": "Food", "weekday": i % 5} for i in range(60)]
    odd = isolation_forest_score({"amount": 5000, "category": "Food", "weekday": 6}, hist)
    norm = isolation_forest_score({"amount": 25, "category": "Food", "weekday": 2}, hist)
    assert odd["available"] and odd["score"] > norm["score"]
