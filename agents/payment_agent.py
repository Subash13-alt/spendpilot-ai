"""Agent 6 - Payment: prepares reimbursements and submits/verifies PayPal Sandbox payouts.

Rules enforced here:
  * a payout can only start from FINANCE_AUTHORIZED (a human decision);
  * an expense becomes PAID only when PayPal's API reports the payout item as SUCCESS;
  * without PayPal credentials the agent runs a labelled LOCAL_DEMO mode that moves no money,
    never sets PAID and never claims completion.
"""
import json
from typing import Optional

from database.connection import transaction
from services.audit_service import TransitionError, log_event, transition
from services.payment_service import PayPalClient, PayPalError, classify_item_status


class PaymentError(Exception):
    pass


class PaymentAgent:
    name = "payment_agent"
    description = "Prepares reimbursements and verifies PayPal Sandbox payouts"

    def __init__(self, client: Optional[PayPalClient] = None):
        self.client = client

    @property
    def mode(self) -> str:
        return "PAYPAL_SANDBOX" if self.client and self.client.configured else "LOCAL_DEMO"

    # ---------------------------------------------------------------- prepare
    def prepare(self, conn, expense_id: int, authorized_by: int) -> dict:
        """Create the reimbursement record once finance has authorised. Idempotent per expense."""
        exp = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if exp is None or exp["status"] != "FINANCE_AUTHORIZED":
            raise PaymentError("Payment can only be prepared for finance-authorised expenses")
        existing = conn.execute("SELECT * FROM reimbursements WHERE expense_id = ?", (expense_id,)).fetchone()
        if existing:
            return dict(existing)
        with transaction(conn):
            conn.execute(
                "INSERT INTO reimbursements (org_id, expense_id, user_id, amount, currency, status, mode, authorized_by)"
                " VALUES (?,?,?,?,?,'PREPARED',?,?)",
                (exp["org_id"], expense_id, exp["user_id"], exp["amount"], exp["currency"], self.mode, authorized_by))
            log_event(conn, exp["org_id"], "PAYMENT_PREPARED", expense_id, None, "AGENT",
                      details={"mode": self.mode, "amount": exp["amount"], "currency": exp["currency"]})
        return dict(conn.execute("SELECT * FROM reimbursements WHERE expense_id = ?", (expense_id,)).fetchone())

    # ---------------------------------------------------------------- submit
    def submit(self, conn, expense_id: int, actor_id: int) -> dict:
        """Start the payout. Returns {mode, status, paid, message}."""
        exp = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if exp is None:
            raise PaymentError("Expense not found")
        if exp["status"] != "FINANCE_AUTHORIZED":
            raise PaymentError(f"Expense is {exp['status']}; a payout requires FINANCE_AUTHORIZED")
        reimb = conn.execute("SELECT * FROM reimbursements WHERE expense_id = ?", (expense_id,)).fetchone()
        if reimb is None:
            reimb = self.prepare(conn, expense_id, actor_id)

        if self.mode == "LOCAL_DEMO":
            with transaction(conn):
                conn.execute("UPDATE reimbursements SET mode='LOCAL_DEMO', status='DEMO_NOT_PAID',"
                             " failure_reason=?, updated_at=datetime('now') WHERE expense_id=?",
                             ("PayPal credentials not configured - no payment was made", expense_id))
                log_event(conn, exp["org_id"], "PAYMENT_DEMO_ONLY", expense_id, actor_id, "USER",
                          details={"note": "Local demonstration mode. No money moved; expense NOT marked PAID."})
            return {"mode": "LOCAL_DEMO", "status": "DEMO_NOT_PAID", "paid": False,
                    "message": "Demo mode: PayPal credentials are not configured. No payment was made and the "
                               "expense is NOT marked paid."}

        recipient = conn.execute("SELECT paypal_email FROM users WHERE id = ?", (exp["user_id"],)).fetchone()["paypal_email"]
        if not recipient:
            return self._fail(conn, exp, "Employee has no PayPal email on file", actor_id, move_status=False)

        # Count previous attempts so each *new* attempt gets a fresh sender_batch_id; a retried
        # attempt that PayPal already accepted would otherwise be rejected as a duplicate (good: no double pay).
        attempt = conn.execute("SELECT COUNT(*) c FROM audit_logs WHERE expense_id = ? AND action = 'PAYOUT_SUBMITTED'",
                               (expense_id,)).fetchone()["c"] + 1
        batch_id = f"SPX-{exp['org_id']}-{expense_id}-{attempt}"
        with transaction(conn):
            transition(conn, expense_id, "PAYMENT_PROCESSING", actor_id, "USER", "PAYOUT_SUBMITTED",
                       {"sender_batch_id": batch_id, "mode": "PAYPAL_SANDBOX"})
            conn.execute("UPDATE reimbursements SET status='PROCESSING', mode='PAYPAL_SANDBOX', sender_batch_id=?,"
                         " failure_reason=NULL, updated_at=datetime('now') WHERE expense_id=?", (batch_id, expense_id))
        # network call happens outside any DB transaction
        try:
            resp = self.client.create_payout(batch_id, f"EXP-{expense_id}", recipient, exp["amount"], exp["currency"],
                                             f"Expense #{expense_id} reimbursement")
        except PayPalError as exc:
            if exc.outcome_unknown:
                with transaction(conn):
                    conn.execute("UPDATE reimbursements SET failure_reason=?, updated_at=datetime('now') WHERE expense_id=?",
                                 (f"{exc}. Check the PayPal sandbox for sender_batch_id {batch_id} before retrying.", expense_id))
                    log_event(conn, exp["org_id"], "PAYOUT_OUTCOME_UNKNOWN", expense_id, None, "AGENT", details=str(exc))
                return {"mode": "PAYPAL_SANDBOX", "status": "PROCESSING", "paid": False,
                        "message": "Submission outcome unknown. The expense stays in PAYMENT_PROCESSING; do not retry "
                                   "until the PayPal sandbox has been checked."}
            return self._fail(conn, exp, str(exc), actor_id)
        header = resp.get("batch_header", {})
        payout_batch_id = header.get("payout_batch_id")
        with transaction(conn):
            conn.execute("UPDATE reimbursements SET payout_batch_id=?, updated_at=datetime('now') WHERE expense_id=?",
                         (payout_batch_id, expense_id))
            log_event(conn, exp["org_id"], "PAYOUT_ACCEPTED_BY_PAYPAL", expense_id, None, "AGENT",
                      details={"payout_batch_id": payout_batch_id, "batch_status": header.get("batch_status")})
        return self.verify(conn, expense_id)

    # ---------------------------------------------------------------- verify
    def verify(self, conn, expense_id: int) -> dict:
        """Ask PayPal for the real status and update SQLite. Only PayPal's answer can make an expense PAID."""
        exp = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        reimb = conn.execute("SELECT * FROM reimbursements WHERE expense_id = ?", (expense_id,)).fetchone()
        if exp is None or reimb is None:
            raise PaymentError("No reimbursement found for this expense")
        if exp["status"] != "PAYMENT_PROCESSING":
            return {"mode": reimb["mode"], "status": reimb["status"], "paid": exp["status"] == "PAID",
                    "message": f"Expense is {exp['status']}; nothing to verify."}
        if self.mode != "PAYPAL_SANDBOX":
            raise PaymentError("PayPal credentials are not configured; cannot verify")
        if not reimb["payout_batch_id"]:
            return {"mode": "PAYPAL_SANDBOX", "status": "PROCESSING", "paid": False,
                    "message": "No PayPal batch id recorded; check the sandbox for sender_batch_id "
                               f"{reimb['sender_batch_id']}."}
        try:
            data = self.client.get_payout(reimb["payout_batch_id"])
        except PayPalError as exc:
            return {"mode": "PAYPAL_SANDBOX", "status": "PROCESSING", "paid": False,
                    "message": f"Verification unavailable right now: {exc}"}
        items = data.get("items") or []
        if not items:
            return {"mode": "PAYPAL_SANDBOX", "status": "PROCESSING", "paid": False,
                    "message": "PayPal has not returned the payout item yet. Refresh shortly."}
        item = items[0]
        raw_status = item.get("transaction_status")
        outcome = classify_item_status(raw_status)
        ref = item.get("transaction_id") or item.get("payout_item_id")
        if outcome == "PAID":
            with transaction(conn):
                conn.execute("UPDATE reimbursements SET status='PAID', payout_item_id=?, transaction_ref=?,"
                             " failure_reason=NULL, updated_at=datetime('now') WHERE expense_id=?",
                             (item.get("payout_item_id"), ref, expense_id))
                transition(conn, expense_id, "PAID", None, "AGENT", "PAYMENT_VERIFIED",
                           {"paypal_status": raw_status, "transaction_ref": ref, "payout_batch_id": reimb["payout_batch_id"]})
            return {"mode": "PAYPAL_SANDBOX", "status": "PAID", "paid": True, "transaction_ref": ref,
                    "message": "Payment verified by PayPal Sandbox."}
        if outcome == "FAILED":
            reason = (item.get("errors") or {}).get("message") or f"PayPal reported {raw_status}"
            return self._fail(conn, exp, reason, None, move_status=True, item=item, system=True)
        with transaction(conn):
            conn.execute("UPDATE reimbursements SET payout_item_id=?, status='PROCESSING', updated_at=datetime('now')"
                         " WHERE expense_id=?", (item.get("payout_item_id"), expense_id))
        return {"mode": "PAYPAL_SANDBOX", "status": "PROCESSING", "paid": False,
                "message": f"PayPal status is {raw_status}; not yet complete."}

    # ---------------------------------------------------------------- helpers
    def _fail(self, conn, exp, reason: str, actor_id, move_status: bool = True, item=None, system=False) -> dict:
        with transaction(conn):
            conn.execute("UPDATE reimbursements SET status='FAILED', failure_reason=?, updated_at=datetime('now')"
                         " WHERE expense_id=?", (reason[:500], exp["id"]))
            current = conn.execute("SELECT status FROM expenses WHERE id = ?", (exp["id"],)).fetchone()["status"]
            if current == "FINANCE_AUTHORIZED":
                transition(conn, exp["id"], "PAYMENT_PROCESSING", actor_id, "USER" if actor_id else "AGENT", "PAYOUT_ATTEMPT")
                current = "PAYMENT_PROCESSING"
            if current == "PAYMENT_PROCESSING":
                transition(conn, exp["id"], "PAYMENT_FAILED", None, "AGENT", "PAYMENT_FAILED", {"reason": reason})
        return {"mode": "PAYPAL_SANDBOX", "status": "FAILED", "paid": False, "message": f"Payment failed: {reason}"}
