"""Expense submission, listing, detail and receipt download."""
import json
import os
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)
from pydantic import BaseModel, ValidationError, field_validator

from agents.orchestrator import get_orchestrator
from database.connection import get_db
from intelligence.expense_classifier import CATEGORY_KEYWORDS
from routes.auth import login_required, roles_required
from services import receipt_service
from services.approval_service import ApprovalError
from services.scope import get_visible_expense, scope_clause

bp = Blueprint("expenses", __name__)

CATEGORIES = list(CATEGORY_KEYWORDS) + ["Other"]
CURRENCIES = ["USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY"]
STATUS_FILTERS = ["DRAFT", "SUBMITTED", "ANALYSING", "FLAGGED", "PENDING_MANAGER_APPROVAL", "MANAGER_APPROVED",
                  "PENDING_FINANCE_AUTHORIZATION", "FINANCE_AUTHORIZED", "PAYMENT_PROCESSING", "PAID",
                  "REJECTED", "PAYMENT_FAILED"]


class ExpenseForm(BaseModel):
    merchant: str
    expense_date: str
    amount: float
    currency: str
    category: str
    description: str = ""

    @field_validator("merchant")
    @classmethod
    def _merchant(cls, v):
        v = " ".join(v.split())
        if not 2 <= len(v) <= 100:
            raise ValueError("Merchant must be 2-100 characters")
        return v

    @field_validator("expense_date")
    @classmethod
    def _date(cls, v):
        try:
            d = datetime.strptime(v.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Date must be YYYY-MM-DD")
        if d > date.today() + timedelta(days=1):
            raise ValueError("Expense date cannot be in the future")
        if d < date.today() - timedelta(days=730):
            raise ValueError("Expense date is more than two years old")
        return d.isoformat()

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        if not (0 < v <= 1_000_000) or round(v, 2) != v:
            raise ValueError("Amount must be positive, at most 1,000,000, with up to 2 decimals")
        return v

    @field_validator("currency")
    @classmethod
    def _cur(cls, v):
        v = v.strip().upper()
        if v not in CURRENCIES:
            raise ValueError("Unsupported currency")
        return v

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v not in CATEGORIES:
            raise ValueError("Unknown category")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        return v.strip()[:300]


def _first_error(exc: ValidationError) -> str:
    e = exc.errors()[0]
    loc = ".".join(str(x) for x in e["loc"])
    return f"{loc}: {e['msg'].replace('Value error, ', '')}"


def _agent():
    return get_orchestrator().agents["expense_agent"]


@bp.route("/expenses")
@login_required
def list_expenses():
    db, user = get_db(), g.user
    where, params = scope_clause(user)
    status = request.args.get("status", "")
    if status and status not in STATUS_FILTERS:
        abort(400, description="Unknown status filter")
    sql = ("SELECT e.id, e.merchant, e.category, e.amount, e.currency, e.expense_date, e.status, u.name AS employee,"
           " d.name AS department, (SELECT risk_level FROM fraud_alerts f WHERE f.expense_id = e.id ORDER BY f.id DESC LIMIT 1) AS risk,"
           " r.status AS reimbursement_status, r.mode AS reimbursement_mode"
           " FROM expenses e JOIN users u ON u.id = e.user_id LEFT JOIN departments d ON d.id = e.department_id"
           " LEFT JOIN reimbursements r ON r.expense_id = e.id"
           f" WHERE {where}")
    if status:
        sql += " AND e.status = ?"
        params.append(status)
    rows = db.execute(sql + " ORDER BY e.id DESC LIMIT 200", params).fetchall()
    return render_template("expenses.html", rows=rows, statuses=STATUS_FILTERS, current=status)


@bp.route("/expenses/new", methods=["GET", "POST"])
@roles_required("employee")
def new_expense():
    if request.method == "GET":
        return render_template("expense_new.html", categories=CATEGORIES, currencies=CURRENCIES, form={},
                               today=date.today().isoformat())
    raw = {k: request.form.get(k, "") for k in ("merchant", "expense_date", "currency", "category", "description")}
    try:
        form = ExpenseForm(amount=_to_float(request.form.get("amount", "")), **raw)
    except (ValidationError, ValueError) as exc:
        flash(_first_error(exc) if isinstance(exc, ValidationError) else str(exc), "danger")
        return render_template("expense_new.html", categories=CATEGORIES, currencies=CURRENCIES, form=raw,
                               today=date.today().isoformat()), 400
    record = {**form.model_dump(), "receipt_path": None, "receipt_hash": None, "extraction": None}
    upload = request.files.get("receipt")
    if upload and upload.filename:
        try:
            # Re-run extraction server-side so "employee corrections" are measured against the real OCR result.
            rec = _agent().analyse_receipt(upload, current_app.config["UPLOAD_FOLDER"], current_app.config["MAX_CONTENT_LENGTH"],
                                           current_app.config["TESSERACT_CMD"])
        except receipt_service.ReceiptError as exc:
            flash(str(exc), "danger")
            return render_template("expense_new.html", categories=CATEGORIES, currencies=CURRENCIES, form=raw,
                                   today=date.today().isoformat()), 400
        record.update(receipt_path=rec["receipt_path"], receipt_hash=rec["receipt_hash"], extraction=rec)
    db, orch = get_db(), get_orchestrator()
    eid = orch.create_expense(db, g.user, record)
    try:
        res = orch.submit_expense(db, eid, g.user)
    except ApprovalError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("expenses.detail", expense_id=eid))
    flash("Expense submitted and analysed. Status: " + res["status"].replace("_", " ").title(), "success")
    return redirect(url_for("expenses.detail", expense_id=eid))


def _to_float(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        raise ValueError("amount: enter a valid number")


@bp.route("/api/expenses/extract", methods=["POST"])
@roles_required("employee")
def extract_preview():
    """Live extraction preview. Nothing is stored; the employee can correct fields before submitting."""
    upload = request.files.get("receipt")
    text = request.form.get("text", "")
    try:
        if upload and upload.filename:
            rec = _agent().analyse_receipt(upload, current_app.config["UPLOAD_FOLDER"], current_app.config["MAX_CONTENT_LENGTH"],
                                           current_app.config["TESSERACT_CMD"], keep=False)
        elif text.strip():
            if len(text) > 5000:
                return jsonify(error="Receipt text is too long"), 400
            rec = _agent().analyse_text(text)
        else:
            return jsonify(error="Provide a receipt file or text"), 400
    except receipt_service.ReceiptError as exc:
        return jsonify(error=str(exc)), 400
    rec.pop("receipt_hash", None)
    return jsonify(rec)


@bp.route("/expenses/<int:expense_id>")
@login_required
def detail(expense_id):
    db, user = get_db(), g.user
    exp = get_visible_expense(db, user, expense_id)
    if exp is None:
        abort(404)
    analyses = {}
    for r in db.execute("SELECT agent, result_json, explanation, created_at FROM ai_analysis WHERE expense_id = ? ORDER BY id", (expense_id,)):
        analyses[r["agent"]] = {"data": json.loads(r["result_json"]), "explanation": r["explanation"], "at": r["created_at"]}
    alerts = db.execute("SELECT * FROM fraud_alerts WHERE expense_id = ? ORDER BY id", (expense_id,)).fetchall()
    approvals = db.execute("SELECT a.*, u.name AS approver FROM approvals a JOIN users u ON u.id = a.approver_id"
                           " WHERE a.expense_id = ? ORDER BY a.id", (expense_id,)).fetchall()
    timeline = db.execute(
        "SELECT l.*, u.name AS actor FROM audit_logs l LEFT JOIN users u ON u.id = l.actor_id WHERE l.expense_id = ?"
        " ORDER BY l.id", (expense_id,)).fetchall()
    reimb = db.execute("SELECT * FROM reimbursements WHERE expense_id = ?", (expense_id,)).fetchone()
    owner = db.execute("SELECT name, email FROM users WHERE id = ?", (exp["user_id"],)).fetchone()
    dept = db.execute("SELECT name FROM departments WHERE id = ?", (exp["department_id"],)).fetchone()
    agent_log = []
    if user["role"] != "employee":
        agent_log = db.execute("SELECT agent_name, status, duration_ms, error, created_at FROM agent_execution_logs"
                               " WHERE expense_id = ? ORDER BY id", (expense_id,)).fetchall()
    return render_template("expense_detail.html", e=exp, analyses=analyses, alerts=alerts, approvals=approvals,
                           timeline=timeline, reimb=reimb, owner=owner, dept=dept, agent_log=agent_log,
                           orch_mode=get_orchestrator().payment.mode)


@bp.route("/expenses/<int:expense_id>/receipt")
@login_required
def receipt(expense_id):
    exp = get_visible_expense(get_db(), g.user, expense_id)
    if exp is None or not exp["receipt_path"]:
        abort(404)
    folder = os.path.realpath(current_app.config["UPLOAD_FOLDER"])
    name = os.path.basename(exp["receipt_path"])
    if not os.path.realpath(os.path.join(folder, name)).startswith(folder + os.sep):
        abort(404)
    return send_from_directory(folder, name, as_attachment=False, mimetype=None)
