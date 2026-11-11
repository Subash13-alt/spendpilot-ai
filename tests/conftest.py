import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.orchestrator import Orchestrator
from database.connection import connect
from database.schema import init_schema
from database.seed import seed_base


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    seed_base(c)
    yield c
    c.close()


def get_user(conn, email):
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


@pytest.fixture
def users(conn):
    return {k: get_user(conn, e) for k, e in {
        "alice": "alice@nexora.com", "bob": "bob@nexora.com", "carol": "carol@nexora.com",
        "mgr_eng": "manager.eng@nexora.com", "mgr_mkt": "manager.mkt@nexora.com",
        "finance": "finance@nexora.com", "admin": "admin@nexora.com", "auditor": "auditor@nexora.com"}.items()}


@pytest.fixture
def orch():
    return Orchestrator(payment_client=None)


def make_expense(conn, orch, user, merchant="Hotel Grand Palace", amount=350.0, category="Accommodation",
                 date="2026-11-09", receipt_hash=None, extraction=None, receipt_path="x.txt"):
    eid = orch.create_expense(conn, user, {
        "merchant": merchant, "expense_date": date, "amount": amount, "currency": "USD", "category": category,
        "receipt_path": receipt_path, "receipt_hash": receipt_hash, "extraction": extraction})
    return eid


def status_of(conn, eid):
    return conn.execute("SELECT status FROM expenses WHERE id = ?", (eid,)).fetchone()["status"]


def push_to_finance_stage(conn, orch, users, eid):
    orch.submit_expense(conn, eid, users["alice"])
    orch.manager_decision(conn, eid, users["mgr_eng"], "APPROVED", "ok")
    return status_of(conn, eid)
