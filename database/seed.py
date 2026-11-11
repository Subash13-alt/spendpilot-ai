"""Demonstration data for NEXORA TECHNOLOGIES. Idempotent: does nothing if the org exists."""
import io
import os
import random
import sqlite3
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from database.connection import transaction

ORG_NAME = "NEXORA TECHNOLOGIES"
DEMO_PASSWORD = "Password123!"
DEPARTMENTS = ["Engineering", "Marketing", "Operations", "Human Resources"]

# (name, email, role, department)
USERS = [
    ("Sam Admin", "admin@nexora.com", "super_admin", None),
    ("Fiona Finance", "finance@nexora.com", "finance_admin", None),
    ("Aria Auditor", "auditor@nexora.com", "auditor", None),
    ("Ethan Engineer-Lead", "manager.eng@nexora.com", "manager", "Engineering"),
    ("Maya Marketing-Lead", "manager.mkt@nexora.com", "manager", "Marketing"),
    ("Omar Operations-Lead", "manager.ops@nexora.com", "manager", "Operations"),
    ("Hana HR-Lead", "manager.hr@nexora.com", "manager", "Human Resources"),
    ("Alice Johnson", "alice@nexora.com", "employee", "Engineering"),
    ("Bob Martin", "bob@nexora.com", "employee", "Engineering"),
    ("Carol Singh", "carol@nexora.com", "employee", "Marketing"),
    ("Dave Lopez", "dave@nexora.com", "employee", "Operations"),
    ("Eve Turner", "eve@nexora.com", "employee", "Human Resources"),
]

# category, limit, prohibited, description
POLICIES = [
    ("Accommodation", 200, 0, "Hotel stays are limited to $200 per night."),
    ("Food", 100, 0, "Meals are limited to $100 per receipt."),
    ("Transportation", 150, 0, "Taxi, flights and fuel are limited to $150 per claim."),
    ("Office Supplies", 250, 0, "Office supplies are limited to $250 per claim."),
    ("Software", 500, 0, "Software subscriptions are limited to $500 per claim."),
    ("Entertainment", 75, 0, "Entertainment is limited to $75 per claim."),
    ("Other", 100, 0, "Uncategorised expenses are limited to $100."),
    ("Alcohol", None, 1, "Alcohol purchases are not reimbursable."),
    ("Gambling", None, 1, "Gambling is a prohibited expense category."),
]

BUDGETS = {  # annual budget per department (2026)
    "Engineering": 20000,
    "Marketing": 3000,
    "Operations": 8000,
    "Human Resources": 5000,
}


def seed_base(conn: sqlite3.Connection, period: str = None) -> bool:
    """Create the demo organisation, users, policies and budgets. Returns True if seeded."""
    period = period or str(date.today().year)
    if conn.execute("SELECT 1 FROM organizations WHERE name = ?", (ORG_NAME,)).fetchone():
        return False
    pw_hash = generate_password_hash(DEMO_PASSWORD)
    with transaction(conn):
        org_id = conn.execute("INSERT INTO organizations (name) VALUES (?)", (ORG_NAME,)).lastrowid
        dept_ids = {}
        for name in DEPARTMENTS:
            dept_ids[name] = conn.execute(
                "INSERT INTO departments (org_id, name) VALUES (?, ?)", (org_id, name)
            ).lastrowid
        for name, email, role, dept in USERS:
            uid = conn.execute(
                "INSERT INTO users (org_id, department_id, name, email, password_hash, role, paypal_email)"
                " VALUES (?,?,?,?,?,?,?)",
                (org_id, dept_ids.get(dept), name, email, pw_hash, role,
                 email if role == "employee" else None),
            ).lastrowid
            if role == "manager":
                conn.execute("UPDATE departments SET manager_id = ? WHERE id = ?", (uid, dept_ids[dept]))
        for category, limit, prohibited, desc in POLICIES:
            conn.execute(
                "INSERT INTO expense_policies (org_id, category, limit_amount, prohibited, description)"
                " VALUES (?,?,?,?,?)", (org_id, category, limit, prohibited, desc))
        for dept, amount in BUDGETS.items():
            conn.execute(
                "INSERT INTO budgets (org_id, department_id, period, amount) VALUES (?,?,?,?)",
                (org_id, dept_ids[dept], period, amount))
    return True


# --------------------------------------------------------------------------- demo workflow data
# (user email, merchant, category, typical amount, count) - historical baseline spending this year
BASELINE = [
    ("alice@nexora.com", ["Cafe Aroma", "Green Bowl Restaurant", "Noodle House Restaurant"], "Food", 24, 9),
    ("bob@nexora.com", ["Urban Bistro", "Taco Corner Restaurant"], "Food", 28, 7),
    ("alice@nexora.com", ["Metro Cab", "City Taxi"], "Transportation", 38, 6),
    ("bob@nexora.com", ["Staples Office Supply"], "Office Supplies", 90, 4),
    ("alice@nexora.com", ["Hotel Lakeview", "Riverside Inn"], "Accommodation", 160, 3),
    ("dave@nexora.com", ["BP Fuel Station", "Metro Cab"], "Transportation", 60, 5),
    ("dave@nexora.com", ["Dell Software License"], "Software", 210, 2),
    ("eve@nexora.com", ["Cafe Aroma"], "Food", 30, 4),
    ("carol@nexora.com", ["Ad Agency Services"], "Other", 85, 3),
    ("carol@nexora.com", ["Canva Software Subscription"], "Software", 240, 4),
    ("carol@nexora.com", ["Expo Booth Rental"], "Other", 95, 4),
    ("carol@nexora.com", ["Print Hub Office Supply"], "Office Supplies", 225, 4),
]
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_receipts")


def seed_baseline_history(conn: sqlite3.Connection) -> int:
    """Insert historical, human-approved spending so anomaly detection and budgets have a baseline.

    These rows are approved-stage only. They are NOT marked PAID: no real payment happened.
    """
    rng = random.Random(7)
    org = conn.execute("SELECT id FROM organizations WHERE name = ?", (ORG_NAME,)).fetchone()["id"]
    jan1, today = date(date.today().year, 1, 1), date.today()
    span = max((today - jan1).days - 12, 1)
    n = 0
    with transaction(conn):
        for email, merchants, category, typical, count in BASELINE:
            u = conn.execute("SELECT id, department_id FROM users WHERE email = ?", (email,)).fetchone()
            for i in range(count):
                amount = round(typical * rng.uniform(0.8, 1.2), 2)
                d = jan1 + timedelta(days=int(span * (i + 1) / (count + 1)))
                eid = conn.execute(
                    "INSERT INTO expenses (org_id, user_id, department_id, merchant, expense_date, amount, currency, category,"
                    " description, status) VALUES (?,?,?,?,?,?,'USD',?,?,'MANAGER_APPROVED')",
                    (org, u["id"], u["department_id"], rng.choice(merchants), d.isoformat(), amount, category,
                     "Seeded historical expense")).lastrowid
                conn.execute("INSERT INTO audit_logs (org_id, expense_id, actor_type, action, new_status, details)"
                             " VALUES (?,?,'SYSTEM','SEEDED_DEMO_DATA','MANAGER_APPROVED',"
                             "'Historical baseline for demo; reimbursement history not modelled')", (org, eid))
                n += 1
    return n


def _receipt_record(agent, filename, upload_folder, description):
    from werkzeug.datastructures import FileStorage
    with open(os.path.join(SAMPLE_DIR, filename), "rb") as fh:
        rec = agent.analyse_receipt(FileStorage(io.BytesIO(fh.read()), filename=filename), upload_folder, 5 * 1024 * 1024)
    return {"merchant": rec["merchant"], "expense_date": rec["date"], "amount": rec["amount"], "currency": rec["currency"],
            "category": rec["category"], "description": description, "receipt_path": rec["receipt_path"],
            "receipt_hash": rec["receipt_hash"], "extraction": rec}


def seed_demo_workflow(conn: sqlite3.Connection, orchestrator, upload_folder: str) -> list:
    """Run the demonstration scenarios through the REAL agent pipeline (nothing is hand-faked)."""
    seed_baseline_history(conn)
    agent = orchestrator.agents["expense_agent"]
    def user(email):
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    scenarios = [
        ("alice@nexora.com", "hotel_grand_palace.txt", "Client visit - hotel (policy violation demo)"),
        ("alice@nexora.com", "hotel_grand_palace.txt", "Re-submitted hotel receipt (duplicate demo)"),
        ("bob@nexora.com", "suspicious_dinner.txt", "Team dinner (suspicious spending demo)"),
        ("carol@nexora.com", "normal_lunch.txt", "Team lunch (normal expense demo)"),
    ]
    out = []
    for email, fname, desc in scenarios:
        u = user(email)
        eid = orchestrator.create_expense(conn, u, _receipt_record(agent, fname, upload_folder, desc))
        res = orchestrator.submit_expense(conn, eid, u)
        out.append((eid, email, fname, res["status"]))
    return out
