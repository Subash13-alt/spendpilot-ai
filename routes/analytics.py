"""Dashboards and analytics. Phase 1: role-based dashboards with core stats."""
from flask import Blueprint, g, redirect, render_template, url_for

from database.connection import get_db
from routes.auth import login_required
from services.scope import scope_clause

bp = Blueprint("analytics", __name__)

DASHBOARD_TEMPLATES = {
    "employee": "employee_dashboard.html",
    "manager": "manager_dashboard.html",
    "finance_admin": "finance_dashboard.html",
    "super_admin": "admin_dashboard.html",
    "auditor": "admin_dashboard.html",
}


@bp.route("/")
def index():
    return redirect(url_for("analytics.dashboard"))


@bp.route("/dashboard")
@login_required
def dashboard():
    db, user = get_db(), g.user
    where, params = scope_clause(user)
    stats = db.execute(
        f"SELECT COUNT(*) AS total, COALESCE(SUM(amount),0) AS spend,"
        f" SUM(status LIKE 'PENDING%') AS pending, SUM(status='PAID') AS paid,"
        f" SUM(status='REJECTED') AS rejected, SUM(status='FLAGGED') AS flagged"
        f" FROM expenses e WHERE {where}", params).fetchone()
    recent = db.execute(
        f"SELECT e.id, e.merchant, e.amount, e.currency, e.category, e.status, e.expense_date"
        f" FROM expenses e WHERE {where} ORDER BY e.id DESC LIMIT 8", params).fetchall()
    extra = {}
    if user["role"] in ("manager", "finance_admin", "super_admin", "auditor"):
        extra["pending_queue"] = db.execute(
            f"SELECT e.id, e.merchant, e.amount, e.status FROM expenses e WHERE {where} AND e.status IN"
            " ('PENDING_MANAGER_APPROVAL','FLAGGED','PENDING_FINANCE_AUTHORIZATION') ORDER BY e.id DESC LIMIT 6", params).fetchall()
        extra["alerts"] = db.execute(
            "SELECT f.expense_id, f.risk_level, f.risk_score, f.reason FROM fraud_alerts f JOIN expenses e ON e.id = f.expense_id"
            f" WHERE f.status = 'OPEN' AND {where} ORDER BY f.risk_score DESC LIMIT 4", params).fetchall()
        dept_id = user["department_id"] if user["role"] == "manager" else None
        extra["budgets"] = get_orchestrator().agents["finance_agent"].budget_status(db, user["org_id"], dept_id)
    if user["role"] == "finance_admin":
        extra["to_pay"] = db.execute(
            "SELECT COUNT(*) c FROM expenses WHERE org_id = ? AND status = 'FINANCE_AUTHORIZED'", (user["org_id"],)).fetchone()["c"]
        extra["payment_mode"] = get_orchestrator().payment.mode
    if user["role"] == "employee":
        extra["reimb"] = db.execute(
            "SELECT e.id, e.merchant, e.amount, e.status FROM expenses e WHERE e.user_id = ? AND e.status IN"
            " ('MANAGER_APPROVED','PENDING_FINANCE_AUTHORIZATION','FINANCE_AUTHORIZED','PAYMENT_PROCESSING','PAYMENT_FAILED','PAID')"
            " ORDER BY e.id DESC LIMIT 5", (user["id"],)).fetchall()
    return render_template(DASHBOARD_TEMPLATES[user["role"]], stats=stats, recent=recent, **extra)


# ----------------------------------------------------------------------------- analytics
from flask import abort, jsonify, request  # noqa: E402

from agents.orchestrator import get_orchestrator  # noqa: E402
from routes.auth import roles_required  # noqa: E402


def _fin():
    return get_orchestrator().agents["finance_agent"]


@bp.route("/analytics")
@roles_required("manager", "finance_admin", "super_admin", "auditor")
def analytics_page():
    return render_template("analytics.html")


@bp.route("/api/analytics/data")
@roles_required("manager", "finance_admin", "super_admin", "auditor")
def analytics_data():
    db, user, fin = get_db(), g.user, _fin()
    dept_id = user["department_id"] if user["role"] == "manager" else None
    return jsonify(
        summary=fin.summary(db, user),
        by_department=fin.by_department(db, user),
        by_category=fin.by_category(db, user),
        budgets=fin.budget_status(db, user["org_id"], dept_id),
        forecast=fin.monthly_forecast(db, user),
    )


@bp.route("/api/query", methods=["POST"])
@login_required
def nl_query():
    """Natural-language financial question -> whitelisted, parameterised query. Never executes user SQL."""
    body = request.get_json(silent=True) or {}
    question = body.get("question", "")
    if not isinstance(question, str):
        return jsonify(error="question must be text"), 400
    return jsonify(_fin().answer_question(get_db(), g.user, question))
