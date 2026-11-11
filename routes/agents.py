"""AI Intelligence dashboard and audit log views."""
import json

from flask import Blueprint, current_app, g, jsonify, render_template, request

from agents.orchestrator import get_orchestrator
from database.connection import get_db
from routes.auth import roles_required

bp = Blueprint("agents_view", __name__)


@bp.route("/agents")
@roles_required("manager", "finance_admin", "super_admin", "auditor")
def dashboard():
    db, user, orch = get_db(), g.user, get_orchestrator()
    org = user["org_id"]
    dept_filter, dparams = "", []
    if user["role"] == "manager":
        dept_filter, dparams = " AND e.department_id = ?", [user["department_id"]]
    statuses = orch.agent_status(db, org)
    logs = db.execute(
        "SELECT l.agent_name, l.status, l.duration_ms, l.error, l.created_at, l.expense_id FROM agent_execution_logs l"
        " LEFT JOIN expenses e ON e.id = l.expense_id WHERE l.org_id = ?" + dept_filter + " ORDER BY l.id DESC LIMIT 40",
        [org] + dparams).fetchall()
    decisions = []
    for r in db.execute(
            "SELECT a.expense_id, a.result_json, a.explanation, a.created_at, e.merchant, e.amount, e.status FROM ai_analysis a"
            " JOIN expenses e ON e.id = a.expense_id WHERE e.org_id = ? AND a.agent = 'decision_engine'" + dept_filter +
            " ORDER BY a.id DESC LIMIT 12", [org] + dparams):
        d = dict(r)
        d["decision"] = json.loads(r["result_json"])
        decisions.append(d)
    alerts = db.execute(
        "SELECT f.*, e.merchant, e.amount FROM fraud_alerts f JOIN expenses e ON e.id = f.expense_id"
        " WHERE f.org_id = ?" + dept_filter + " ORDER BY f.id DESC LIMIT 12", [org] + dparams).fetchall()
    violations = db.execute(
        "SELECT a.expense_id, a.result_json, e.merchant FROM ai_analysis a JOIN expenses e ON e.id = a.expense_id"
        " WHERE e.org_id = ? AND a.agent = 'policy_agent'" + dept_filter + " ORDER BY a.id DESC LIMIT 60", [org] + dparams).fetchall()
    viol = []
    for v in violations:
        j = json.loads(v["result_json"])
        if j.get("status") not in ("COMPLIANT",):
            viol.append({"expense_id": v["expense_id"], "merchant": v["merchant"], **j})
    return render_template("agents.html", statuses=statuses, logs=logs, decisions=decisions, alerts=alerts,
                           violations=viol[:10], nlp=current_app.extensions.get("nlp_status", {}),
                           payment_mode=orch.payment.mode)


@bp.route("/api/agents/status")
@roles_required("manager", "finance_admin", "super_admin", "auditor")
def status_api():
    return jsonify(agents=get_orchestrator().agent_status(get_db(), g.user["org_id"]))


@bp.route("/audit")
@roles_required("auditor", "super_admin")
def audit():
    db, user = get_db(), g.user
    page = max(1, request.args.get("page", 1, type=int))
    per = 50
    action = request.args.get("action", "").strip()[:60]
    expense_id = request.args.get("expense_id", type=int)
    sql = ("FROM audit_logs l LEFT JOIN users u ON u.id = l.actor_id WHERE l.org_id = ?")
    params = [user["org_id"]]
    if action:
        sql += " AND l.action = ?"
        params.append(action)
    if expense_id:
        sql += " AND l.expense_id = ?"
        params.append(expense_id)
    total = db.execute("SELECT COUNT(*) c " + sql, params).fetchone()["c"]
    rows = db.execute("SELECT l.*, u.name AS actor " + sql + " ORDER BY l.id DESC LIMIT ? OFFSET ?", params + [per, (page - 1) * per]).fetchall()
    actions = [r["action"] for r in db.execute("SELECT DISTINCT action FROM audit_logs WHERE org_id = ? ORDER BY action", (user["org_id"],))]
    return render_template("audit.html", rows=rows, page=page, pages=max(1, -(-total // per)), total=total,
                           actions=actions, action=action, expense_id=expense_id)
