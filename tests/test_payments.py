import pytest
import requests

from agents.orchestrator import Orchestrator
from agents.payment_agent import PaymentError
from services.approval_service import ApprovalError
from services.payment_service import PayPalClient, PayPalError, classify_item_status
from tests.conftest import make_expense, push_to_finance_stage, status_of


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class FakeSession:
    """Scripted stand-in for requests.Session. Records calls; no network is ever used."""

    def __init__(self, payout_status="SUCCESS", create_resp=None, post_exc=None, get_exc=None, items=True):
        self.calls, self.payout_status, self.post_exc, self.get_exc, self.items = [], payout_status, post_exc, get_exc, items
        self.create_resp = create_resp

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/v1/oauth2/token"):
            return FakeResp(200, {"access_token": "tok", "expires_in": 3000})
        if self.post_exc:
            raise self.post_exc
        return self.create_resp or FakeResp(201, {"batch_header": {"payout_batch_id": "PB123", "batch_status": "PENDING"}})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if self.get_exc:
            raise self.get_exc
        items = [{"payout_item_id": "PI9", "transaction_id": "TX777", "transaction_status": self.payout_status}] if self.items else []
        return FakeResp(200, {"batch_header": {"batch_status": "SUCCESS"}, "items": items})


def sandbox_orch(session):
    client = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com", session=session)
    return Orchestrator(payment_client=client)


def authorized_expense(conn, orch, users):
    eid = make_expense(conn, orch, users["alice"])
    push_to_finance_stage(conn, orch, users, eid)
    orch.finance_decision(conn, eid, users["finance"], "APPROVED", "ok")
    return eid


def test_demo_mode_never_claims_payment(conn, orch, users):
    eid = authorized_expense(conn, orch, users)
    res = orch.send_payment(conn, eid, users["finance"])
    assert res["mode"] == "LOCAL_DEMO" and res["paid"] is False
    assert status_of(conn, eid) == "FINANCE_AUTHORIZED"  # NOT paid
    r = conn.execute("SELECT * FROM reimbursements WHERE expense_id=?", (eid,)).fetchone()
    assert r["status"] == "DEMO_NOT_PAID" and r["mode"] == "LOCAL_DEMO" and r["transaction_ref"] is None


def test_sandbox_success_verified_by_paypal(conn, users):
    sess = FakeSession("SUCCESS")
    orch = sandbox_orch(sess)
    eid = authorized_expense(conn, orch, users)
    res = orch.send_payment(conn, eid, users["finance"])
    assert res["paid"] is True and status_of(conn, eid) == "PAID"
    r = conn.execute("SELECT * FROM reimbursements WHERE expense_id=?", (eid,)).fetchone()
    assert (r["status"], r["transaction_ref"], r["payout_batch_id"]) == ("PAID", "TX777", "PB123")
    post = [c for c in sess.calls if c[0] == "POST" and c[1].endswith("/payouts")][0]
    item = post[2]["json"]["items"][0]
    assert item["receiver"] == "alice@nexora.com" and item["amount"] == {"value": "350.00", "currency": "USD"}
    assert post[2]["json"]["sender_batch_header"]["sender_batch_id"].startswith("SPX-")
    assert any(c[0] == "GET" for c in sess.calls)  # status verified via server-side lookup
    actions = [a["action"] for a in conn.execute("SELECT action FROM audit_logs WHERE expense_id=?", (eid,))]
    assert "PAYMENT_VERIFIED" in actions and "PAYOUT_SUBMITTED" in actions


def test_pending_then_success_after_refresh(conn, users):
    sess = FakeSession("PENDING")
    orch = sandbox_orch(sess)
    eid = authorized_expense(conn, orch, users)
    res = orch.send_payment(conn, eid, users["finance"])
    assert res["paid"] is False and status_of(conn, eid) == "PAYMENT_PROCESSING"
    sess.payout_status = "SUCCESS"
    assert orch.refresh_payment(conn, eid, users["finance"])["paid"] is True
    assert status_of(conn, eid) == "PAID"


def test_failed_payout_then_retry_gets_new_batch_id(conn, users):
    sess = FakeSession("FAILED")
    orch = sandbox_orch(sess)
    eid = authorized_expense(conn, orch, users)
    assert orch.send_payment(conn, eid, users["finance"])["status"] == "FAILED"
    assert status_of(conn, eid) == "PAYMENT_FAILED"
    first = conn.execute("SELECT sender_batch_id FROM reimbursements WHERE expense_id=?", (eid,)).fetchone()[0]
    assert orch.retry_payment(conn, eid, users["finance"]) == "FINANCE_AUTHORIZED"
    sess.payout_status = "SUCCESS"
    assert orch.send_payment(conn, eid, users["finance"])["paid"] is True
    second = conn.execute("SELECT sender_batch_id FROM reimbursements WHERE expense_id=?", (eid,)).fetchone()[0]
    assert first != second


def test_paypal_rejection_marks_failed(conn, users):
    sess = FakeSession(create_resp=FakeResp(422, {"message": "Receiver unregistered"}))
    orch = sandbox_orch(sess)
    eid = authorized_expense(conn, orch, users)
    res = orch.send_payment(conn, eid, users["finance"])
    assert res["status"] == "FAILED" and "Receiver unregistered" in res["message"]
    assert status_of(conn, eid) == "PAYMENT_FAILED"


def test_timeout_leaves_outcome_unknown_not_failed_or_paid(conn, users):
    sess = FakeSession(post_exc=requests.Timeout())
    orch = sandbox_orch(sess)
    eid = authorized_expense(conn, orch, users)
    res = orch.send_payment(conn, eid, users["finance"])
    assert res["paid"] is False and "unknown" in res["message"].lower()
    assert status_of(conn, eid) == "PAYMENT_PROCESSING"


def test_missing_paypal_email_fails_cleanly(conn, users):
    conn.execute("UPDATE users SET paypal_email = NULL WHERE id = ?", (users["alice"]["id"],))
    orch = sandbox_orch(FakeSession())
    eid = authorized_expense(conn, orch, users)
    assert orch.send_payment(conn, eid, users["finance"])["status"] == "FAILED"
    assert status_of(conn, eid) == "PAYMENT_FAILED"


def test_payment_requires_finance_role_and_authorisation(conn, users):
    orch = sandbox_orch(FakeSession())
    eid = make_expense(conn, orch, users["alice"])
    push_to_finance_stage(conn, orch, users, eid)   # manager approved, NOT finance authorised
    with pytest.raises(PaymentError):
        orch.send_payment(conn, eid, users["finance"])
    assert status_of(conn, eid) == "PENDING_FINANCE_AUTHORIZATION"
    orch.finance_decision(conn, eid, users["finance"], "APPROVED")
    for role in ("alice", "mgr_eng", "admin", "auditor"):
        with pytest.raises(ApprovalError):
            orch.send_payment(conn, eid, users[role])


def test_cannot_pay_twice(conn, users):
    orch = sandbox_orch(FakeSession("SUCCESS"))
    eid = authorized_expense(conn, orch, users)
    orch.send_payment(conn, eid, users["finance"])
    with pytest.raises(PaymentError):
        orch.send_payment(conn, eid, users["finance"])


def test_client_restricted_to_sandbox_and_status_mapping():
    with pytest.raises(ValueError):
        PayPalClient("a", "b", "https://api-m.paypal.com")
    assert classify_item_status("SUCCESS") == "PAID"
    for s in ("FAILED", "RETURNED", "BLOCKED", "REVERSED"):
        assert classify_item_status(s) == "FAILED"
    for s in ("PENDING", "ONHOLD", "UNCLAIMED", None):
        assert classify_item_status(s) == "PENDING"


def test_credentials_never_logged_in_audit(conn, users):
    orch = sandbox_orch(FakeSession("SUCCESS"))
    eid = authorized_expense(conn, orch, users)
    orch.send_payment(conn, eid, users["finance"])
    blob = " ".join(str(dict(r)) for r in conn.execute("SELECT * FROM audit_logs")) + \
           " ".join(str(dict(r)) for r in conn.execute("SELECT * FROM agent_execution_logs"))
    assert "secret" not in blob and "Bearer" not in blob
