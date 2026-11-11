import sqlite3

import pytest

from app import create_app
from database.connection import connect, transaction
from database.schema import init_schema
from database.seed import seed_base

EXPECTED_TABLES = {
    "organizations", "users", "departments", "expenses", "expense_policies", "approvals",
    "ai_analysis", "fraud_alerts", "budgets", "reimbursements", "audit_logs", "agent_execution_logs",
}


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def client(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "app.db"), "UPLOAD_FOLDER": str(tmp_path / "up"),
                      "TESTING": True, "CSRF_ENABLED": False})
    return app.test_client()


def test_all_tables_created(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_seed_is_idempotent(conn):
    assert seed_base(conn) is True
    assert seed_base(conn) is False
    assert conn.execute("SELECT COUNT(*) c FROM departments").fetchone()["c"] == 4
    assert conn.execute("SELECT COUNT(*) c FROM users WHERE role='manager'").fetchone()["c"] == 4


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (org_id, name, email, password_hash, role) VALUES (999,'x','x@x.com','h','employee')")


def test_unique_email(conn):
    seed_base(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (org_id, name, email, password_hash, role) VALUES (1,'x','alice@nexora.com','h','employee')")


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO organizations (name) VALUES ('Temp')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT 1 FROM organizations WHERE name='Temp'").fetchone() is None


def test_passwords_are_hashed(conn):
    seed_base(conn)
    pw = conn.execute("SELECT password_hash FROM users WHERE email='alice@nexora.com'").fetchone()[0]
    assert pw != "Password123!" and pw.startswith(("scrypt:", "pbkdf2:"))


def test_login_and_role_dashboard(client):
    assert client.get("/dashboard").status_code == 302
    r = client.post("/login", data={"email": "alice@nexora.com", "password": "Password123!"})
    assert r.status_code == 302
    page = client.get("/dashboard")
    assert page.status_code == 200 and b"My dashboard" in page.data


def test_bad_login_rejected(client):
    r = client.post("/login", data={"email": "alice@nexora.com", "password": "wrong"})
    assert r.status_code == 401


def test_registration_only_creates_employee(client):
    client.post("/register", data={"name": "New Person", "email": "new@x.com", "password": "Passw0rdOK", "department_id": 1,
                                   "role": "super_admin"})
    r = client.post("/login", data={"email": "new@x.com", "password": "Passw0rdOK"})
    assert r.status_code == 302
    assert b"My dashboard" in client.get("/dashboard").data


def test_csrf_enforced(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "c.db"), "UPLOAD_FOLDER": str(tmp_path / "u"), "TESTING": True})
    r = app.test_client().post("/login", data={"email": "alice@nexora.com", "password": "Password123!"})
    assert r.status_code == 400
