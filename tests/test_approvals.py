import json

import pytest

from agents.orchestrator import Orchestrator
from services import approval_service
from services.approval_service import ApprovalError
from services.audit_service import TransitionError, transition
from tests.conftest import make_expense, push_to_finance_stage, status_of


def test_demo_hotel_scenario_runs_full_pipeline(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"], receipt_hash="h1", extraction={"merchant": "Hotel Grand Palace"})
    res = orch.submit_expense(conn, eid, users["alice"])
    assert res["status"] == "PENDING_MANAGER_APPROVAL"
    assert res["results"]["policy_agent"]["status"] == "VIOLATION"
    assert res["results"]["policy_agent"]["excess_amount"] == 150
    assert res["decision"]["human_approval_required"] is True
    assert res["decision"]["recommendation"] == "REVIEW"
    agents_logged = {r["agent_name"] for r in conn.execute("SELECT agent_name FROM agent_execution_logs WHERE expense_id=?", (eid,))}
    assert {"expense_agent", "policy_agent", "fraud_agent", "finance_agent", "approval_agent"} <= agents_logged
    assert conn.execute("SELECT COUNT(*) c FROM ai_analysis WHERE expense_id=?", (eid,)).fetchone()["c"] == 6


def test_ai_never_reaches_approval_states_on_its_own(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"], merchant="Cafe", amount=12, category="Food")
    orch.submit_expense(conn, eid, users["alice"])
    assert status_of(conn, eid) == "PENDING_MANAGER_APPROVAL"  # even a perfect expense waits for a human
    for target in ("MANAGER_APPROVED", "FINANCE_AUTHORIZED"):
        with pytest.raises(TransitionError):
            transition(conn, eid, target, None, "AGENT")


def test_duplicate_receipt_is_flagged_with_alert(conn, orch, users):
    a = make_expense(conn, orch, users["alice"], receipt_hash="same", date="2026-10-01")
    orch.submit_expense(conn, a, users["alice"])
    b = make_expense(conn, orch, users["alice"], receipt_hash="same", date="2026-10-02")
    res = orch.submit_expense(conn, b, users["alice"])
    assert res["status"] == "FLAGGED" and res["results"]["fraud_agent"]["risk_level"] == "HIGH"
    alert = conn.execute("SELECT * FROM fraud_alerts WHERE expense_id=?", (b,)).fetchone()
    assert alert["status"] == "OPEN" and alert["risk_score"] >= 70


def test_prohibited_category_flagged(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"], merchant="Lucky Casino", amount=40, category="Gambling")
    assert orch.submit_expense(conn, eid, users["alice"])["status"] == "FLAGGED"


def test_agent_failure_routes_to_human_review_not_approval(conn, users, monkeypatch):
    orch = Orchestrator()
    monkeypatch.setattr(orch.agents["policy_agent"], "run", lambda c, s: (_ for _ in ()).throw(RuntimeError("boom")))
    eid = make_expense(conn, orch, users["alice"], merchant="Cafe", amount=12, category="Food")
    res = orch.submit_expense(conn, eid, users["alice"])
    assert res["status"] == "FLAGGED" and res["errors"][0]["agent"] == "policy_agent"
    row = conn.execute("SELECT status, error FROM agent_execution_logs WHERE expense_id=? AND agent_name='policy_agent'", (eid,)).fetchone()
    assert row["status"] == "ERROR" and "boom" in row["error"]


def test_only_owner_can_submit(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    with pytest.raises(ApprovalError):
        orch.submit_expense(conn, eid, users["bob"])
    orch.submit_expense(conn, eid, users["alice"])
    with pytest.raises(ApprovalError):
        orch.submit_expense(conn, eid, users["alice"])  # no double submit


def test_full_human_workflow_to_authorized(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    assert push_to_finance_stage(conn, orch, users, eid) == "PENDING_FINANCE_AUTHORIZATION"
    assert orch.finance_decision(conn, eid, users["finance"], "APPROVED", "authorised") == "FINANCE_AUTHORIZED"
    reimb = conn.execute("SELECT * FROM reimbursements WHERE expense_id=?", (eid,)).fetchone()
    assert reimb["status"] == "PREPARED" and reimb["authorized_by"] == users["finance"]["id"]
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_logs WHERE expense_id=? ORDER BY id", (eid,))]
    for a in ("EXPENSE_CREATED", "EXPENSE_SUBMITTED", "AI_ANALYSIS_COMPLETE", "MANAGER_APPROVED", "ROUTED_TO_FINANCE",
              "FINANCE_AUTHORIZED", "PAYMENT_PREPARED"):
        assert a in actions
    assert conn.execute("SELECT COUNT(*) c FROM approvals WHERE expense_id=?", (eid,)).fetchone()["c"] == 2


def test_manager_permission_rules(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    orch.submit_expense(conn, eid, users["alice"])
    with pytest.raises(ApprovalError):  # other department
        orch.manager_decision(conn, eid, users["mgr_mkt"], "APPROVED")
    for role in ("alice", "finance", "admin", "auditor"):
        with pytest.raises(ApprovalError):
            orch.manager_decision(conn, eid, users[role], "APPROVED")
    with pytest.raises(ApprovalError):
        orch.manager_decision(conn, eid, users["mgr_eng"], "MAYBE")
    assert status_of(conn, eid) == "PENDING_MANAGER_APPROVAL"


def test_finance_cannot_skip_manager_and_roles_enforced(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    orch.submit_expense(conn, eid, users["alice"])
    with pytest.raises(ApprovalError):
        orch.finance_decision(conn, eid, users["finance"], "APPROVED")  # not yet at finance stage
    orch.manager_decision(conn, eid, users["mgr_eng"], "APPROVED")
    for role in ("mgr_eng", "alice", "admin", "auditor"):
        with pytest.raises(ApprovalError):
            orch.finance_decision(conn, eid, users[role], "APPROVED")


def test_flagged_requires_comment(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"], merchant="Lucky Casino", amount=40, category="Gambling")
    orch.submit_expense(conn, eid, users["alice"])
    with pytest.raises(ApprovalError):
        orch.manager_decision(conn, eid, users["mgr_eng"], "APPROVED", "")
    assert orch.manager_decision(conn, eid, users["mgr_eng"], "REJECTED", "Not allowed") == "REJECTED"
    assert status_of(conn, eid) == "REJECTED"


def test_flag_review_is_audited_when_manager_approves(conn, orch, users):
    a = make_expense(conn, orch, users["alice"], receipt_hash="dup", date="2026-10-01")
    orch.submit_expense(conn, a, users["alice"])
    b = make_expense(conn, orch, users["alice"], receipt_hash="dup", date="2026-10-02")
    orch.submit_expense(conn, b, users["alice"])
    assert status_of(conn, b) == "FLAGGED"
    orch.manager_decision(conn, b, users["mgr_eng"], "APPROVED", "Verified with employee: legitimate re-submission")
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_logs WHERE expense_id=?", (b,))]
    assert "FLAG_REVIEWED" in actions
    assert conn.execute("SELECT status FROM fraud_alerts WHERE expense_id=?", (b,)).fetchone()["status"] == "REVIEWED"


def test_self_approval_blocked(conn, orch, users):
    eid = orch.create_expense(conn, users["mgr_eng"], {"merchant": "Cafe", "expense_date": "2026-11-01", "amount": 20,
                                                       "currency": "USD", "category": "Food"})
    orch.submit_expense(conn, eid, users["mgr_eng"])
    with pytest.raises(ApprovalError, match="own expense"):
        orch.manager_decision(conn, eid, users["mgr_eng"], "APPROVED")


def test_illegal_and_terminal_transitions(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    with pytest.raises(TransitionError):
        transition(conn, eid, "PAID", users["finance"]["id"], "USER")
    orch.submit_expense(conn, eid, users["alice"])
    orch.manager_decision(conn, eid, users["mgr_eng"], "REJECTED", "no")
    with pytest.raises(TransitionError):
        transition(conn, eid, "PENDING_MANAGER_APPROVAL", users["mgr_eng"]["id"], "USER")


def test_organisation_isolation(conn, orch, users):
    from werkzeug.security import generate_password_hash
    org2 = conn.execute("INSERT INTO organizations (name) VALUES ('OTHER CORP')").lastrowid
    dep2 = conn.execute("INSERT INTO departments (org_id, name) VALUES (?, 'Engineering')", (org2,)).lastrowid
    mid = conn.execute("INSERT INTO users (org_id, department_id, name, email, password_hash, role) VALUES (?,?,?,?,?,'manager')",
                       (org2, dep2, "Other Mgr", "m@other.com", generate_password_hash("x"))).lastrowid
    other = conn.execute("SELECT * FROM users WHERE id=?", (mid,)).fetchone()
    eid = make_expense(conn, orch, users["alice"])
    orch.submit_expense(conn, eid, users["alice"])
    with pytest.raises(ApprovalError) as ei:
        orch.manager_decision(conn, eid, other, "APPROVED")
    assert ei.value.http_status == 404
    from agents.finance_agent import FinancialIntelligenceAgent
    res = FinancialIntelligenceAgent().answer_question(conn, other, "Show all expenses above $10")
    assert res["rows"] == []


def test_agent_history_and_status(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    orch.submit_expense(conn, eid, users["alice"])
    orch.manager_decision(conn, eid, users["mgr_eng"], "APPROVED", "fine")
    hist = orch.agents["approval_agent"].history(conn, eid)
    assert hist[0]["stage"] == "MANAGER" and hist[0]["approver"] == users["mgr_eng"]["name"]
    status = {s["name"]: s for s in orch.agent_status(conn, users["alice"]["org_id"])}
    assert len(status) == 6 and status["policy_agent"]["runs"] == 1 and status["payment_agent"]["state"] == "idle"
