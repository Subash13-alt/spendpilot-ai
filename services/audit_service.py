"""Audit trail and the single gatekeeper for expense status transitions."""
import json
from typing import Optional

# Every legal status change. Anything not listed here is rejected.
ALLOWED_TRANSITIONS = {
    "DRAFT": {"SUBMITTED"},
    "SUBMITTED": {"ANALYSING"},
    "ANALYSING": {"FLAGGED", "PENDING_MANAGER_APPROVAL"},
    "FLAGGED": {"PENDING_MANAGER_APPROVAL", "REJECTED"},
    "PENDING_MANAGER_APPROVAL": {"MANAGER_APPROVED", "REJECTED"},
    "MANAGER_APPROVED": {"PENDING_FINANCE_AUTHORIZATION"},
    "PENDING_FINANCE_AUTHORIZATION": {"FINANCE_AUTHORIZED", "REJECTED"},
    "FINANCE_AUTHORIZED": {"PAYMENT_PROCESSING"},
    "PAYMENT_PROCESSING": {"PAID", "PAYMENT_FAILED"},
    "PAYMENT_FAILED": {"FINANCE_AUTHORIZED"},  # finance may retry
    "PAID": set(),
    "REJECTED": set(),
}

# Statuses only a human (never an agent) may cause.
HUMAN_ONLY_TARGETS = {"MANAGER_APPROVED", "FINANCE_AUTHORIZED"}


class TransitionError(Exception):
    pass


def log_event(conn, org_id: int, action: str, expense_id: Optional[int] = None,
              actor_id: Optional[int] = None, actor_type: str = "USER",
              old_status: Optional[str] = None, new_status: Optional[str] = None,
              details=None) -> None:
    if details is not None and not isinstance(details, str):
        details = json.dumps(details, default=str)
    conn.execute(
        "INSERT INTO audit_logs (org_id, expense_id, actor_id, actor_type, action,"
        " old_status, new_status, details) VALUES (?,?,?,?,?,?,?,?)",
        (org_id, expense_id, actor_id, actor_type, action, old_status, new_status, details),
    )


def transition(conn, expense_id: int, new_status: str, actor_id: Optional[int] = None,
               actor_type: str = "USER", action: str = "STATUS_CHANGE", details=None) -> str:
    """Move an expense to new_status if legal, writing an audit row. Returns the old status.

    AI/system actors can never produce a human-only status (approvals/authorisation).
    """
    row = conn.execute("SELECT org_id, status FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if row is None:
        raise TransitionError(f"Expense {expense_id} not found")
    old = row["status"]
    if new_status not in ALLOWED_TRANSITIONS.get(old, set()):
        raise TransitionError(f"Illegal transition {old} -> {new_status}")
    if new_status in HUMAN_ONLY_TARGETS and (actor_type != "USER" or actor_id is None):
        raise TransitionError(f"{new_status} requires a human actor")
    conn.execute("UPDATE expenses SET status = ?, updated_at = datetime('now') WHERE id = ?",
                 (new_status, expense_id))
    log_event(conn, row["org_id"], action, expense_id, actor_id, actor_type, old, new_status, details)
    return old
