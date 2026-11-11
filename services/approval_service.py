"""Human approval decisions. Enforces role, organisation, department and separation-of-duties rules."""
from database.connection import transaction
from services.audit_service import TransitionError, log_event, transition


class ApprovalError(Exception):
    """Raised when a user is not allowed to make the requested decision."""

    def __init__(self, message, http_status=403):
        super().__init__(message)
        self.http_status = http_status


def _load(conn, expense_id, user):
    exp = conn.execute("SELECT * FROM expenses WHERE id = ? AND org_id = ?", (expense_id, user["org_id"])).fetchone()
    if exp is None:  # also hides expenses of other organisations
        raise ApprovalError("Expense not found", 404)
    return exp


def _clean_comment(comment):
    return (comment or "").strip()[:500]


def manager_decide(conn, expense_id: int, user, decision: str, comment: str = "") -> str:
    """Manager approves/rejects. FLAGGED expenses require a written comment (the manager takes ownership)."""
    if decision not in ("APPROVED", "REJECTED"):
        raise ApprovalError("Decision must be APPROVED or REJECTED", 400)
    if user["role"] != "manager":
        raise ApprovalError("Only department managers can make manager decisions")
    comment = _clean_comment(comment)
    with transaction(conn):
        exp = _load(conn, expense_id, user)
        if exp["department_id"] != user["department_id"]:
            raise ApprovalError("This expense belongs to another department")
        if exp["user_id"] == user["id"]:
            raise ApprovalError("You cannot approve your own expense")
        if exp["status"] not in ("PENDING_MANAGER_APPROVAL", "FLAGGED"):
            raise ApprovalError(f"Expense is {exp['status']} and is not awaiting manager review", 409)
        if exp["status"] == "FLAGGED":
            if not comment:
                raise ApprovalError("A comment is required to decide on a flagged expense", 400)
            if decision == "APPROVED":  # manager explicitly takes the flag into review first
                transition(conn, expense_id, "PENDING_MANAGER_APPROVAL", user["id"], "USER", "FLAG_REVIEWED", {"comment": comment})
        conn.execute("INSERT INTO approvals (expense_id, approver_id, stage, decision, comment) VALUES (?,?,?,?,?)",
                     (expense_id, user["id"], "MANAGER", decision, comment))
        if decision == "REJECTED":
            transition(conn, expense_id, "REJECTED", user["id"], "USER", "MANAGER_REJECTED", {"comment": comment})
            return "REJECTED"
        transition(conn, expense_id, "MANAGER_APPROVED", user["id"], "USER", "MANAGER_APPROVED", {"comment": comment})
        # system routing step: hand over to finance. Authorisation itself stays human-only.
        transition(conn, expense_id, "PENDING_FINANCE_AUTHORIZATION", None, "SYSTEM", "ROUTED_TO_FINANCE")
        return "PENDING_FINANCE_AUTHORIZATION"


def finance_decide(conn, expense_id: int, user, decision: str, comment: str = "") -> str:
    """Finance administrator authorises (or rejects) reimbursement. Never callable by an agent."""
    if decision not in ("APPROVED", "REJECTED"):
        raise ApprovalError("Decision must be APPROVED or REJECTED", 400)
    if user["role"] != "finance_admin":
        raise ApprovalError("Only finance administrators can authorise reimbursements")
    comment = _clean_comment(comment)
    with transaction(conn):
        exp = _load(conn, expense_id, user)
        if exp["user_id"] == user["id"]:
            raise ApprovalError("You cannot authorise your own expense")
        if exp["status"] != "PENDING_FINANCE_AUTHORIZATION":
            raise ApprovalError(f"Expense is {exp['status']} and is not awaiting finance authorisation", 409)
        conn.execute("INSERT INTO approvals (expense_id, approver_id, stage, decision, comment) VALUES (?,?,?,?,?)",
                     (expense_id, user["id"], "FINANCE", decision, comment))
        if decision == "REJECTED":
            transition(conn, expense_id, "REJECTED", user["id"], "USER", "FINANCE_REJECTED", {"comment": comment})
            return "REJECTED"
        transition(conn, expense_id, "FINANCE_AUTHORIZED", user["id"], "USER", "FINANCE_AUTHORIZED", {"comment": comment})
        return "FINANCE_AUTHORIZED"


def retry_failed_payment(conn, expense_id: int, user) -> str:
    """Finance explicitly re-authorises a payment that failed."""
    if user["role"] != "finance_admin":
        raise ApprovalError("Only finance administrators can retry payments")
    with transaction(conn):
        exp = _load(conn, expense_id, user)
        if exp["status"] != "PAYMENT_FAILED":
            raise ApprovalError("Only failed payments can be retried", 409)
        transition(conn, expense_id, "FINANCE_AUTHORIZED", user["id"], "USER", "PAYMENT_RETRY_AUTHORIZED")
        conn.execute("UPDATE reimbursements SET status = 'PREPARED', failure_reason = NULL,"
                     " updated_at = datetime('now') WHERE expense_id = ?", (expense_id,))
        return "FINANCE_AUTHORIZED"


def mark_alerts_reviewed(conn, expense_id: int, user, status: str = "REVIEWED"):
    if user["role"] not in ("manager", "finance_admin", "super_admin"):
        raise ApprovalError("Not allowed to review alerts")
    if status not in ("REVIEWED", "DISMISSED"):
        raise ApprovalError("Invalid alert status", 400)
    with transaction(conn):
        _load(conn, expense_id, user)
        conn.execute("UPDATE fraud_alerts SET status = ? WHERE expense_id = ? AND org_id = ?",
                     (status, expense_id, user["org_id"]))
        log_event(conn, user["org_id"], f"FRAUD_ALERT_{status}", expense_id, user["id"])
