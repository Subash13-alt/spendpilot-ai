"""Payment tracking and finance payout actions."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from agents.orchestrator import get_orchestrator
from agents.payment_agent import PaymentError
from database.connection import get_db
from routes.auth import roles_required
from services.approval_service import ApprovalError
from services.audit_service import TransitionError

bp = Blueprint("payments", __name__)


@bp.route("/payments")
@roles_required("finance_admin", "super_admin", "auditor")
def overview():
    db, user, orch = get_db(), g.user, get_orchestrator()
    rows = db.execute(
        "SELECT e.id, e.merchant, e.amount, e.currency, e.status AS expense_status, u.name AS employee, u.paypal_email,"
        " r.status AS reimb_status, r.mode, r.sender_batch_id, r.payout_batch_id, r.transaction_ref, r.failure_reason, r.updated_at"
        " FROM expenses e JOIN users u ON u.id = e.user_id LEFT JOIN reimbursements r ON r.expense_id = e.id"
        " WHERE e.org_id = ? AND e.status IN ('FINANCE_AUTHORIZED','PAYMENT_PROCESSING','PAID','PAYMENT_FAILED')"
        " ORDER BY e.id DESC", (user["org_id"],)).fetchall()
    return render_template("payments.html", rows=rows, mode=orch.payment.mode)


def _run(action, expense_id, ok_message=None):
    db, user, orch = get_db(), g.user, get_orchestrator()
    try:
        res = action(db, expense_id, user)
        if isinstance(res, dict):
            flash(res["message"], "success" if res.get("paid") else "warning")
        else:
            flash(ok_message or "Done.", "success")
    except (ApprovalError, PaymentError, TransitionError) as exc:
        flash(str(exc), "danger")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") and
                    not (request.form.get("next") or "").startswith("//") else url_for("payments.overview"))


@bp.route("/payments/<int:expense_id>/send", methods=["POST"])
@roles_required("finance_admin")
def send(expense_id):
    return _run(get_orchestrator().send_payment, expense_id)


@bp.route("/payments/<int:expense_id>/refresh", methods=["POST"])
@roles_required("finance_admin")
def refresh(expense_id):
    return _run(get_orchestrator().refresh_payment, expense_id)


@bp.route("/payments/<int:expense_id>/retry", methods=["POST"])
@roles_required("finance_admin")
def retry(expense_id):
    return _run(get_orchestrator().retry_payment, expense_id, "Payment re-authorised. Use 'Send payout' to retry.")
