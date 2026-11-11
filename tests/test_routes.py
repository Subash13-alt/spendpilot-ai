"""HTTP-level integration tests: every page per role, RBAC, CSRF, upload validation and the full demo flow."""
import io
import re

import pytest

from app import create_app
from database.connection import connect

PW = "Password123!"
ROLES = {"employee": "alice@nexora.com", "manager": "manager.eng@nexora.com", "finance": "finance@nexora.com",
         "admin": "admin@nexora.com", "auditor": "auditor@nexora.com"}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("web")
    return create_app({"DATABASE": str(tmp / "w.db"), "UPLOAD_FOLDER": str(tmp / "up"), "TESTING": True})


class Client:
    """Test client that handles the CSRF token like a browser would."""

    def __init__(self, app, email):
        self.c = app.test_client()
        self.token()
        r = self.c.post("/login", data={"email": email, "password": PW, "_csrf": self.tok})
        assert r.status_code == 302, r.data[:200]
        self.token()

    def token(self):
        html = self.c.get("/login" if "_csrf" not in self.__dict__ else "/dashboard", follow_redirects=True).data.decode()
        self.tok = re.search(r'name="csrf-token" content="([0-9a-f]+)"', html) or re.search(r'name="_csrf" value="([0-9a-f]+)"', html)
        self.tok = self.tok.group(1)

    def get(self, *a, **k):
        return self.c.get(*a, **k)

    def post(self, url, data=None, **k):
        data = dict(data or {})
        data["_csrf"] = self.tok
        return self.c.post(url, data=data, **k)


@pytest.fixture(scope="module")
def clients(app):
    return {k: Client(app, e) for k, e in ROLES.items()}


PAGES = {
    "employee": ["/dashboard", "/expenses", "/expenses/new"],
    "manager": ["/dashboard", "/expenses", "/approvals", "/analytics", "/agents", "/budgets", "/api/analytics/data"],
    "finance": ["/dashboard", "/expenses", "/approvals", "/payments", "/analytics", "/agents", "/budgets", "/api/analytics/data"],
    "admin": ["/dashboard", "/expenses", "/payments", "/analytics", "/agents", "/budgets", "/audit", "/admin"],
    "auditor": ["/dashboard", "/expenses", "/payments", "/analytics", "/agents", "/audit"],
}


@pytest.mark.parametrize("role", PAGES)
def test_every_page_renders_for_its_role(clients, role):
    for path in PAGES[role]:
        r = clients[role].get(path)
        assert r.status_code == 200, f"{role} {path} -> {r.status_code}"


FORBIDDEN = {
    "employee": ["/approvals", "/payments", "/analytics", "/agents", "/admin", "/audit", "/budgets"],
    "manager": ["/payments", "/admin", "/audit", "/expenses/new"],
    "finance": ["/admin", "/audit", "/expenses/new"],
    "admin": ["/approvals", "/expenses/new"],
    "auditor": ["/approvals", "/admin", "/budgets", "/expenses/new"],
}


@pytest.mark.parametrize("role", FORBIDDEN)
def test_backend_blocks_other_roles(clients, role):
    for path in FORBIDDEN[role]:
        assert clients[role].get(path).status_code == 403, f"{role} should not access {path}"


def test_unauthenticated_redirected_and_api_401(app):
    c = app.test_client()
    assert c.get("/dashboard").status_code == 302
    assert c.get("/api/analytics/data").status_code == 401
    assert c.get("/expenses/1").status_code == 302


def test_demo_data_loaded_through_real_pipeline(clients):
    c = clients["manager"]
    q = c.get("/approvals").data.decode()
    assert "Hotel Grand Palace" in q or "Lucky Star" in q
    a = c.get("/agents").data.decode()
    assert "Expense Agent" in a and "Policy Agent" in a and "Payment Agent" in a


def test_employee_sees_only_own_expenses(clients):
    html = clients["employee"].get("/expenses").data.decode()
    assert "Hotel Grand Palace" in html and "Lucky Star" not in html and "Cafe Roma" not in html


def test_employee_cannot_open_others_expense_or_receipt(app, clients):
    conn = connect(app.config["DATABASE"])
    other = conn.execute("SELECT e.id FROM expenses e JOIN users u ON u.id=e.user_id WHERE u.email='carol@nexora.com' LIMIT 1").fetchone()["id"]
    assert clients["employee"].get(f"/expenses/{other}").status_code == 404
    assert clients["employee"].get(f"/expenses/{other}/receipt").status_code == 404
    assert clients["manager"].get(f"/expenses/{other}").status_code == 404  # other department


def test_csrf_missing_rejected(clients):
    r = clients["manager"].c.post("/approvals/1/decide", data={"decision": "APPROVED"})
    assert r.status_code == 400


def test_extract_preview_and_validation(clients):
    c = clients["employee"]
    r = c.post("/api/expenses/extract", data={"text": "Hotel Grand Palace. Date: 09/11/2026. Total: $350.00"})
    j = r.get_json()
    assert (j["merchant"], j["date"], j["amount"], j["category"]) == ("Hotel Grand Palace", "2026-11-09", 350.0, "Accommodation")
    assert c.post("/api/expenses/extract", data={}).status_code == 400
    bad = c.post("/api/expenses/extract", data={"receipt": (io.BytesIO(b"MZ\x90"), "evil.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 400 and "Unsupported" in bad.get_json()["error"]


def test_submit_validation_errors(clients):
    c = clients["employee"]
    base = {"merchant": "Cafe", "expense_date": "2026-11-01", "amount": "12.50", "currency": "USD", "category": "Food"}
    for patch in ({"amount": "-5"}, {"amount": "abc"}, {"currency": "XXX"}, {"category": "Hacking"},
                  {"expense_date": "2099-01-01"}, {"merchant": "x"}, {"amount": "1.234"}):
        r = c.post("/expenses/new", data={**base, **patch}, content_type="multipart/form-data")
        assert r.status_code == 400, patch


def test_full_demo_flow_over_http(app, clients):
    emp, mgr, fin = clients["employee"], clients["manager"], clients["finance"]
    receipt = open("sample_receipts/hotel_grand_palace.png", "rb").read()
    r = emp.post("/expenses/new", data={"merchant": "Hotel Grand Palace", "expense_date": "2026-11-09", "amount": "350.00",
                                        "currency": "USD", "category": "Accommodation", "description": "Demo",
                                        "receipt": (io.BytesIO(receipt), "hotel.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    eid = int(re.search(r"/expenses/(\d+)", r.headers["Location"]).group(1))
    page = emp.get(f"/expenses/{eid}").data.decode()
    assert "VIOLATION" in page and "150.00" in page and "Fraud risk assessment" in page
    assert "Manager decision" not in page                                    # employee has no action panel
    assert emp.post(f"/approvals/{eid}/decide", data={"decision": "APPROVED"}).status_code == 403
    # manager approves
    mgr.post(f"/approvals/{eid}/decide", data={"decision": "APPROVED", "comment": "ok, client trip"})
    assert "Finance authorization" in fin.get(f"/expenses/{eid}").data.decode()
    # finance authorises, then (demo mode) payout does not pay
    fin.post(f"/approvals/{eid}/decide", data={"decision": "APPROVED"})
    fin.post(f"/payments/{eid}/send", data={})
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT status FROM expenses WHERE id=?", (eid,)).fetchone()[0] == "FINANCE_AUTHORIZED"
    assert conn.execute("SELECT status FROM reimbursements WHERE expense_id=?", (eid,)).fetchone()[0] == "DEMO_NOT_PAID"
    emp_view = emp.get(f"/expenses/{eid}").data.decode()
    assert "DEMO" in emp_view and "no money moved" in emp_view
    aud = clients["auditor"].get(f"/audit?expense_id={eid}").data.decode()
    for act in ("EXPENSE_SUBMITTED", "MANAGER_APPROVED", "FINANCE_AUTHORIZED", "PAYMENT_DEMO_ONLY"):
        assert act in aud


def test_natural_language_queries_over_http(clients):
    c = clients["finance"]
    def ask(q):
        return c.c.post("/api/query", json={"question": q}, headers={"X-CSRF-Token": c.tok}).get_json()
    assert ask("What is the total engineering expenditure?")["intent"] == "department_total"
    assert ask("Which department has the highest spending?")["intent"] == "top_department"
    r = ask("Show all expenses above $500")
    assert r["intent"] == "expenses_above" and all(float(x[3].strip("$").replace(",", "")) > 500 for x in r["rows"])
    assert ask("How much budget is remaining?")["intent"] == "budget_remaining"
    assert ask("Which employees have pending reimbursements?")["intent"] == "pending_reimbursements"
    inj = ask("'; DROP TABLE expenses; --")
    assert inj["ok"] is False
    assert ask("Show all expenses above $1; DROP TABLE users")["intent"] == "expenses_above"
    assert c.get("/dashboard").status_code == 200  # tables intact


def test_budget_warning_visible(clients):
    html = clients["finance"].get("/budgets").data.decode()
    assert "budget warning" in html


def test_admin_user_management_and_guards(app, clients):
    adm = clients["admin"]
    r = adm.post("/admin/users", data={"name": "Newbie", "email": "newbie@nexora.com", "password": "Passw0rd1", "role": "employee", "department_id": "2"})
    assert r.status_code == 302
    conn = connect(app.config["DATABASE"])
    me = conn.execute("SELECT id FROM users WHERE email='admin@nexora.com'").fetchone()[0]
    adm.post(f"/admin/users/{me}", data={"role": "employee", "department_id": "1", "is_active": "1"})
    assert conn.execute("SELECT role FROM users WHERE id=?", (me,)).fetchone()[0] == "super_admin"   # cannot self-demote
    adm.post("/admin/policies/natural", data={"text": "Meals are limited to $80 per receipt."})
    assert conn.execute("SELECT limit_amount FROM expense_policies WHERE category='Food'").fetchone()[0] == 80.0
    assert clients["manager"].post("/admin/users", data={}).status_code == 403


def test_xss_is_escaped(app, clients):
    emp = clients["employee"]
    r = emp.post("/expenses/new", data={"merchant": "<script>alert(1)</script> Cafe", "expense_date": "2026-11-02", "amount": "5.00",
                                        "currency": "USD", "category": "Food"}, content_type="multipart/form-data")
    page = emp.get(r.headers["Location"]).data.decode()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
