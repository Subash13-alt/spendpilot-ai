"""Administration: users, departments, policies (incl. natural-language), budgets and organisation settings.

Administration is scoped to the signed-in user's own organisation. Cross-organisation management is
intentionally not exposed so organisation data isolation holds even for Super Admins.
"""
import re

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from pydantic import BaseModel, ValidationError, field_validator
from werkzeug.security import generate_password_hash

from agents.finance_agent import current_period
from config import Config
from database.connection import get_db, transaction
from intelligence.policy_engine import parse_policy_text
from routes.auth import EMAIL_RE, roles_required
from routes.expenses import CATEGORIES
from services.audit_service import log_event

bp = Blueprint("admin", __name__)


def _back(anchor=""):
    return redirect(url_for("admin.admin_home") + anchor)


class NewUser(BaseModel):
    name: str
    email: str
    password: str
    role: str
    department_id: int | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        v = v.strip()
        if not 2 <= len(v) <= 80:
            raise ValueError("Name must be 2-80 characters")
        return v

    @field_validator("email")
    @classmethod
    def _e(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Invalid email")
        return v

    @field_validator("password")
    @classmethod
    def _p(cls, v):
        if len(v) < 8 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("Password needs 8+ characters with letters and digits")
        return v

    @field_validator("role")
    @classmethod
    def _r(cls, v):
        if v not in Config.ROLES:
            raise ValueError("Unknown role")
        return v


@bp.route("/admin")
@roles_required("super_admin")
def admin_home():
    db, org = get_db(), g.user["org_id"]
    return render_template(
        "admin.html",
        org=db.execute("SELECT * FROM organizations WHERE id = ?", (org,)).fetchone(),
        users=db.execute("SELECT u.*, d.name AS department FROM users u LEFT JOIN departments d ON d.id = u.department_id"
                         " WHERE u.org_id = ? ORDER BY u.role, u.name", (org,)).fetchall(),
        departments=db.execute("SELECT d.*, u.name AS manager FROM departments d LEFT JOIN users u ON u.id = d.manager_id"
                               " WHERE d.org_id = ? ORDER BY d.name", (org,)).fetchall(),
        policies=db.execute("SELECT * FROM expense_policies WHERE org_id = ? ORDER BY category", (org,)).fetchall(),
        roles=Config.ROLES, categories=CATEGORIES)


@bp.route("/admin/org", methods=["POST"])
@roles_required("super_admin")
def rename_org():
    name = " ".join(request.form.get("name", "").split())
    if not 2 <= len(name) <= 80:
        flash("Organisation name must be 2-80 characters", "danger")
        return _back()
    db = get_db()
    if db.execute("SELECT 1 FROM organizations WHERE name = ? AND id != ?", (name, g.user["org_id"])).fetchone():
        flash("That organisation name is taken", "danger")
        return _back()
    with transaction(db):
        db.execute("UPDATE organizations SET name = ? WHERE id = ?", (name, g.user["org_id"]))
        log_event(db, g.user["org_id"], "ORG_RENAMED", actor_id=g.user["id"], details={"name": name})
    flash("Organisation updated", "success")
    return _back()


def _dept_in_org(db, dept_id):
    if dept_id is None:
        return None
    row = db.execute("SELECT id FROM departments WHERE id = ? AND org_id = ?", (dept_id, g.user["org_id"])).fetchone()
    return row["id"] if row else False


@bp.route("/admin/users", methods=["POST"])
@roles_required("super_admin")
def create_user():
    db = get_db()
    try:
        raw_dept = request.form.get("department_id", "")
        form = NewUser(name=request.form.get("name", ""), email=request.form.get("email", ""),
                       password=request.form.get("password", ""), role=request.form.get("role", ""),
                       department_id=int(raw_dept) if raw_dept.isdigit() else None)
    except ValidationError as exc:
        flash(exc.errors()[0]["msg"].replace("Value error, ", ""), "danger")
        return _back("#users")
    dept = _dept_in_org(db, form.department_id)
    if dept is False or (form.role in ("manager", "employee") and dept is None):
        flash("A valid department is required for managers and employees", "danger")
        return _back("#users")
    if db.execute("SELECT 1 FROM users WHERE email = ?", (form.email,)).fetchone():
        flash("Email already registered", "danger")
        return _back("#users")
    with transaction(db):
        uid = db.execute("INSERT INTO users (org_id, department_id, name, email, password_hash, role, paypal_email)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (g.user["org_id"], dept, form.name, form.email, generate_password_hash(form.password), form.role,
                          form.email if form.role == "employee" else None)).lastrowid
        if form.role == "manager":
            db.execute("UPDATE departments SET manager_id = ? WHERE id = ?", (uid, dept))
        log_event(db, g.user["org_id"], "USER_CREATED", actor_id=g.user["id"], details={"email": form.email, "role": form.role})
    flash(f"User {form.email} created", "success")
    return _back("#users")


@bp.route("/admin/users/<int:user_id>", methods=["POST"])
@roles_required("super_admin")
def update_user(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ? AND org_id = ?", (user_id, g.user["org_id"])).fetchone()
    if target is None:
        flash("User not found", "danger")
        return _back("#users")
    role = request.form.get("role", target["role"])
    active = 1 if request.form.get("is_active") == "1" else 0
    paypal = request.form.get("paypal_email", "").strip().lower() or None
    raw_dept = request.form.get("department_id", "")
    dept = _dept_in_org(db, int(raw_dept)) if raw_dept.isdigit() else None
    if role not in Config.ROLES or dept is False or (paypal and not EMAIL_RE.match(paypal)):
        flash("Invalid role, department or PayPal email", "danger")
        return _back("#users")
    if role in ("manager", "employee") and dept is None:
        flash("Managers and employees need a department", "danger")
        return _back("#users")
    if target["id"] == g.user["id"] and (role != "super_admin" or not active):
        flash("You cannot demote or deactivate yourself", "danger")
        return _back("#users")
    with transaction(db):
        db.execute("UPDATE users SET role = ?, is_active = ?, department_id = ?, paypal_email = ? WHERE id = ?",
                   (role, active, dept, paypal, user_id))
        db.execute("UPDATE departments SET manager_id = NULL WHERE manager_id = ? AND (? != 'manager' OR id != ?)",
                   (user_id, role, dept or -1))
        if role == "manager":
            db.execute("UPDATE departments SET manager_id = ? WHERE id = ?", (user_id, dept))
        log_event(db, g.user["org_id"], "USER_UPDATED", actor_id=g.user["id"],
                  details={"user_id": user_id, "role": role, "active": active})
    flash("User updated", "success")
    return _back("#users")


@bp.route("/admin/departments", methods=["POST"])
@roles_required("super_admin")
def create_department():
    name = " ".join(request.form.get("name", "").split())
    db = get_db()
    if not 2 <= len(name) <= 60:
        flash("Department name must be 2-60 characters", "danger")
        return _back("#departments")
    if db.execute("SELECT 1 FROM departments WHERE org_id = ? AND lower(name) = lower(?)", (g.user["org_id"], name)).fetchone():
        flash("Department already exists", "danger")
        return _back("#departments")
    with transaction(db):
        db.execute("INSERT INTO departments (org_id, name) VALUES (?, ?)", (g.user["org_id"], name))
        log_event(db, g.user["org_id"], "DEPARTMENT_CREATED", actor_id=g.user["id"], details={"name": name})
    flash("Department created", "success")
    return _back("#departments")


def _save_policy(db, category, limit, prohibited, description):
    db.execute(
        "INSERT INTO expense_policies (org_id, category, limit_amount, prohibited, description) VALUES (?,?,?,?,?)"
        " ON CONFLICT(org_id, category) DO UPDATE SET limit_amount = excluded.limit_amount,"
        " prohibited = excluded.prohibited, description = excluded.description",
        (g.user["org_id"], category, limit, 1 if prohibited else 0, description))
    log_event(db, g.user["org_id"], "POLICY_SAVED", actor_id=g.user["id"],
              details={"category": category, "limit": limit, "prohibited": prohibited})


@bp.route("/admin/policies", methods=["POST"])
@roles_required("super_admin")
def save_policy():
    db = get_db()
    category = request.form.get("category", "")
    prohibited = request.form.get("prohibited") == "1"
    try:
        limit = None if prohibited else float(request.form.get("limit_amount", ""))
    except ValueError:
        flash("Enter a numeric limit or mark the category prohibited", "danger")
        return _back("#policies")
    if category not in CATEGORIES or (limit is not None and not 0 < limit <= 1_000_000):
        flash("Invalid category or limit", "danger")
        return _back("#policies")
    with transaction(db):
        _save_policy(db, category, limit, prohibited, request.form.get("description", "")[:200] or None)
    flash(f"Policy for {category} saved", "success")
    return _back("#policies")


@bp.route("/admin/policies/natural", methods=["POST"])
@roles_required("super_admin")
def save_policy_natural():
    """Interpret a plain-English policy with spaCy, then store the structured rule."""
    text = request.form.get("text", "").strip()
    if not 8 <= len(text) <= 300:
        flash("Describe the policy in 8-300 characters", "danger")
        return _back("#policies")
    try:
        p = parse_policy_text(text)
    except ValueError as exc:
        flash(str(exc), "danger")
        return _back("#policies")
    with transaction(get_db()):
        _save_policy(get_db(), p["category"], p["limit_amount"], p["prohibited"], text)
    meaning = "prohibited" if p["prohibited"] else f"limit ${p['limit_amount']:,.2f}"
    flash(f"Interpreted as: {p['category']} - {meaning}. Saved.", "success")
    return _back("#policies")


@bp.route("/admin/policies/<int:policy_id>/delete", methods=["POST"])
@roles_required("super_admin")
def delete_policy(policy_id):
    db = get_db()
    with transaction(db):
        cur = db.execute("DELETE FROM expense_policies WHERE id = ? AND org_id = ?", (policy_id, g.user["org_id"]))
        if cur.rowcount:
            log_event(db, g.user["org_id"], "POLICY_DELETED", actor_id=g.user["id"], details={"policy_id": policy_id})
    flash("Policy removed", "success")
    return _back("#policies")


# ------------------------------------------------------------------------------ budgets
@bp.route("/budgets", methods=["GET", "POST"])
@roles_required("finance_admin", "super_admin", "manager")
def budgets():
    from agents.orchestrator import get_orchestrator
    db, user = get_db(), g.user
    period = request.values.get("period", current_period())
    if not re.fullmatch(r"\d{4}", period):
        period = current_period()
    if request.method == "POST":
        if user["role"] == "manager":
            flash("Managers have read-only access to budgets", "danger")
            return redirect(url_for("admin.budgets", period=period))
        try:
            amount = float(request.form.get("amount", ""))
            dept = _dept_in_org(db, int(request.form.get("department_id", "0")))
        except ValueError:
            amount, dept = -1, False
        if not dept or not 0 <= amount <= 100_000_000:
            flash("Choose a department and a valid amount", "danger")
        else:
            with transaction(db):
                db.execute("INSERT INTO budgets (org_id, department_id, period, amount) VALUES (?,?,?,?)"
                           " ON CONFLICT(department_id, period) DO UPDATE SET amount = excluded.amount",
                           (user["org_id"], dept, period, amount))
                log_event(db, user["org_id"], "BUDGET_SET", actor_id=user["id"],
                          details={"department_id": dept, "period": period, "amount": amount})
            flash("Budget saved", "success")
        return redirect(url_for("admin.budgets", period=period))
    dept_id = user["department_id"] if user["role"] == "manager" else None
    rows = get_orchestrator().agents["finance_agent"].budget_status(db, user["org_id"], dept_id, period)
    return render_template("budgets.html", rows=rows, period=period, can_edit=user["role"] != "manager")
