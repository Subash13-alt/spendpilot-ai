"""Manager and finance approval queues and decisions."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from agents.orchestrator import get_orchestrator
from database.connection import get_db
from routes.auth import roles_required
from services.approval_service import ApprovalError
from services.audit_service import TransitionError
from services.scope import scope_clause

bp = Blueprint("approvals", __name__)

QUEUE_STATUSES = {"manager": ("PENDING_MANAGER_APPROVAL", "FLAGGED"),
                  "finance_admin": ("PENDING_FINANCE_AUTHORIZATION",)}


@bp.route("/approvals")
@roles_required("manager", "finance_admin")
def queue():
    db, user = get_db(), g.user
    statuses = QUEUE_STATUSES[user["role"]]
    where, params = scope_clause(user)
    marks = ",".join("?" * len(statuses))
    rows = db.execute(
        "SELECT e.id, e.merchant, e.category, e.amount, e.currency, e.expense_date, e.status, u.name AS employee,"
        " d.name AS department,"
        " (SELECT result_json FROM ai_analysis a WHERE a.expense_id = e.id AND a.agent = 'decision_engine' ORDER BY a.id DESC LIMIT 1) AS decision_json,"
        " (SELECT result_json FROM ai_analysis a WHERE a.expense_id = e.id AND a.agent = 'policy_agent' ORDER BY a.id DESC LIMIT 1) AS policy_json,"
        " (SELECT risk_level || ' ' || risk_score FROM fraud_alerts f WHERE f.expense_id = e.id ORDER BY f.id DESC LIMIT 1) AS risk"
        " FROM expenses e JOIN users u ON u.id = e.user_id LEFT JOIN departments d ON d.id = e.department_id"
        f" WHERE {where} AND e.status IN ({marks}) ORDER BY (e.status = 'FLAGGED') DESC, e.id", params + list(statuses)).fetchall()
    import json
    items = []
    for r in rows:
        item = dict(r)
        item["decision"] = json.loads(r["decision_json"]) if r["decision_json"] else {}
        item["policy"] = json.loads(r["policy_json"]) if r["policy_json"] else {}
        items.append(item)
    return render_template("approvals.html", items=items, stage="MANAGER" if user["role"] == "manager" else "FINANCE")


@bp.route("/approvals/<int:expense_id>/decide", methods=["POST"])
@roles_required("manager", "finance_admin")
def decide(expense_id):
    decision = request.form.get("decision", "")
    comment = request.form.get("comment", "")
    db, user, orch = get_db(), g.user, get_orchestrator()
    try:
        if user["role"] == "manager":
            new = orch.manager_decision(db, expense_id, user, decision, comment)
        else:
            new = orch.finance_decision(db, expense_id, user, decision, comment)
        flash(f"Decision recorded. Expense is now {new.replace('_', ' ').title()}.", "success")
    except ApprovalError as exc:
        flash(str(exc), "danger")
    except TransitionError as exc:
        flash(f"Workflow rule: {exc}", "danger")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") and
                    not (request.form.get("next") or "").startswith("//") else url_for("approvals.queue"))
