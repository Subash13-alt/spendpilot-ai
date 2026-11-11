"""Central AI orchestrator.

Coordinates the six agents over one shared WorkflowState, records every agent run,
handles agent failures safely (a failure routes the expense to human review, never to approval),
and resumes the workflow after human decisions. The orchestrator can only perform *system*
transitions; approvals and payment authorisation are human-only (enforced in audit_service).
"""
import json
import logging
from typing import Optional

from flask import current_app

from agents.approval_agent import ApprovalIntelligenceAgent
from agents.base import WorkflowState
from agents.expense_agent import ExpenseIntelligenceAgent
from agents.finance_agent import FinancialIntelligenceAgent
from agents.fraud_agent import FraudDetectionAgent
from agents.payment_agent import PaymentAgent, PaymentError
from agents.policy_agent import PolicyComplianceAgent
from database.connection import transaction
from services import approval_service
from services.audit_service import TransitionError, log_event, transition
from services.payment_service import client_from_config

log = logging.getLogger(__name__)

AGENT_ORDER = ["expense_agent", "policy_agent", "fraud_agent", "finance_agent", "approval_agent"]
ALL_AGENTS = ["expense_agent", "policy_agent", "fraud_agent", "approval_agent", "finance_agent", "payment_agent"]


class Orchestrator:
    def __init__(self, payment_client=None):
        self.agents = {
            "expense_agent": ExpenseIntelligenceAgent(),
            "policy_agent": PolicyComplianceAgent(),
            "fraud_agent": FraudDetectionAgent(),
            "finance_agent": FinancialIntelligenceAgent(),
            "approval_agent": ApprovalIntelligenceAgent(),
        }
        self.payment = PaymentAgent(payment_client)

    # ------------------------------------------------------------------ expense creation + analysis
    def create_expense(self, conn, user, record: dict) -> int:
        """Insert a DRAFT expense for the user (organisation and department come from the account)."""
        with transaction(conn):
            eid = conn.execute(
                "INSERT INTO expenses (org_id, user_id, department_id, merchant, expense_date, amount, currency,"
                " category, description, receipt_path, receipt_hash, extraction_json, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'DRAFT')",
                (user["org_id"], user["id"], user["department_id"], record["merchant"], record["expense_date"],
                 record["amount"], record["currency"], record["category"], record.get("description"),
                 record.get("receipt_path"), record.get("receipt_hash"),
                 json.dumps(record["extraction"]) if record.get("extraction") is not None else None)).lastrowid
            log_event(conn, user["org_id"], "EXPENSE_CREATED", eid, user["id"], "USER", None, "DRAFT",
                      {"amount": record["amount"], "category": record["category"]})
        return eid

    def submit_expense(self, conn, expense_id: int, user) -> dict:
        """Employee submission -> full AI pipeline -> pending human approval (or FLAGGED)."""
        exp = conn.execute("SELECT * FROM expenses WHERE id = ? AND org_id = ?", (expense_id, user["org_id"])).fetchone()
        if exp is None or exp["user_id"] != user["id"]:
            raise approval_service.ApprovalError("Expense not found", 404)
        if exp["status"] != "DRAFT":
            raise approval_service.ApprovalError(f"Expense already {exp['status']}", 409)
        with transaction(conn):
            transition(conn, expense_id, "SUBMITTED", user["id"], "USER", "EXPENSE_SUBMITTED")
            transition(conn, expense_id, "ANALYSING", None, "SYSTEM", "AI_ANALYSIS_STARTED")
        return self._analyse(conn, expense_id, user["id"])

    def _analyse(self, conn, expense_id: int, actor_id: Optional[int]) -> dict:
        exp = dict(conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone())
        state = WorkflowState(expense_id=expense_id, org_id=exp["org_id"], actor_id=actor_id, expense=exp)
        for name in AGENT_ORDER:
            self._run_agent(conn, state, name)
        state.decision = self._decide(state)
        self._persist(conn, state)
        return {"expense_id": expense_id, "status": state.decision["route_to"], "decision": state.decision,
                "results": {k: _public(v) for k, v in state.results.items()}, "errors": state.errors}

    def _run_agent(self, conn, state: WorkflowState, name: str):
        agent = self.agents[name]
        summary = {"expense_id": state.expense_id, "amount": state.expense["amount"], "category": state.expense["category"]}
        try:
            out = agent.execute(conn, state)
            state.results[name] = out
            state.completed_agents.append(name)
            status, err, duration, payload = "SUCCESS", None, out.get("_duration_ms"), _public(out)
        except Exception as exc:  # an agent failure must never become an implicit approval
            log.exception("Agent %s failed on expense %s", name, state.expense_id)
            state.errors.append({"agent": name, "error": f"{type(exc).__name__}: {exc}"})
            status, err, duration, payload = "ERROR", f"{type(exc).__name__}: {exc}", None, None
        conn.execute(
            "INSERT INTO agent_execution_logs (org_id, expense_id, agent_name, status, input_json, output_json,"
            " duration_ms, error) VALUES (?,?,?,?,?,?,?,?)",
            (state.org_id, state.expense_id, name, status, json.dumps(summary),
             json.dumps(payload, default=str) if payload is not None else None, duration, err))

    # ------------------------------------------------------------------ central decision engine
    def _decide(self, state: WorkflowState) -> dict:
        r = state.results
        flags = []
        if state.errors:
            flags.append("AI analysis incomplete (" + ", ".join(e["agent"] for e in state.errors) + "): manual review required")
        if r.get("policy_agent", {}).get("status") == "PROHIBITED":
            flags.append("Prohibited expense category")
        if r.get("fraud_agent", {}).get("risk_level") == "HIGH":
            flags.append("High risk score: " + r["fraud_agent"]["reason"])
        appr = r.get("approval_agent", {})
        route = "FLAGGED" if flags else "PENDING_MANAGER_APPROVAL"
        parts = [r[k]["explanation"] for k in ("expense_agent", "policy_agent", "fraud_agent", "finance_agent") if k in r and r[k].get("explanation")]
        if appr:
            parts.append(appr["explanation"])
        if state.errors:
            parts.append("Errors: " + "; ".join(f"{e['agent']}: {e['error']}" for e in state.errors))
        return {
            "route_to": route, "flag_reasons": flags,
            "recommendation": appr.get("recommendation", "REVIEW"),
            "human_approval_required": True,   # invariant: AI never authorises spend or payment
            "assigned_manager_id": appr.get("assigned_manager_id"),
            "explanation": " | ".join(parts),
        }

    def _persist(self, conn, state: WorkflowState):
        with transaction(conn):
            for name, out in state.results.items():
                conn.execute("INSERT INTO ai_analysis (expense_id, agent, result_json, explanation) VALUES (?,?,?,?)",
                             (state.expense_id, name, json.dumps(_public(out), default=str), out.get("explanation")))
            conn.execute("INSERT INTO ai_analysis (expense_id, agent, result_json, explanation) VALUES (?,?,?,?)",
                         (state.expense_id, "decision_engine", json.dumps(state.decision, default=str), state.decision["explanation"]))
            fraud = state.results.get("fraud_agent")
            if fraud and fraud["risk_level"] in ("MEDIUM", "HIGH"):
                conn.execute("INSERT INTO fraud_alerts (org_id, expense_id, risk_level, risk_score, reason, recommendation)"
                             " VALUES (?,?,?,?,?,?)", (state.org_id, state.expense_id, fraud["risk_level"],
                                                      fraud["risk_score"], fraud["reason"], fraud["recommendation"]))
            transition(conn, state.expense_id, state.decision["route_to"], None, "AGENT", "AI_ANALYSIS_COMPLETE",
                       {"recommendation": state.decision["recommendation"], "flags": state.decision["flag_reasons"]})

    # ------------------------------------------------------------------ human decisions + resume
    def manager_decision(self, conn, expense_id: int, user, decision: str, comment: str = "") -> str:
        new = approval_service.manager_decide(conn, expense_id, user, decision, comment)
        approval_service.mark_alerts_reviewed(conn, expense_id, user, "REVIEWED")
        self._log_orch(conn, user["org_id"], expense_id, "RESUMED_AFTER_MANAGER", {"decision": decision, "status": new})
        return new

    def finance_decision(self, conn, expense_id: int, user, decision: str, comment: str = "") -> str:
        new = approval_service.finance_decide(conn, expense_id, user, decision, comment)
        if new == "FINANCE_AUTHORIZED":
            self.payment.prepare(conn, expense_id, user["id"])   # payment preparation, not payment
        self._log_orch(conn, user["org_id"], expense_id, "RESUMED_AFTER_FINANCE", {"decision": decision, "status": new})
        return new

    def send_payment(self, conn, expense_id: int, user) -> dict:
        self._require_finance(conn, expense_id, user)
        try:
            res = self.payment.submit(conn, expense_id, user["id"])
        except TransitionError as exc:
            raise PaymentError(str(exc))
        self._log_orch(conn, user["org_id"], expense_id, "PAYMENT_STEP", res)
        return res

    def refresh_payment(self, conn, expense_id: int, user) -> dict:
        self._require_finance(conn, expense_id, user)
        res = self.payment.verify(conn, expense_id)
        self._log_orch(conn, user["org_id"], expense_id, "PAYMENT_VERIFY", res)
        return res

    def retry_payment(self, conn, expense_id: int, user) -> str:
        return approval_service.retry_failed_payment(conn, expense_id, user)

    @staticmethod
    def _require_finance(conn, expense_id, user):
        if user["role"] != "finance_admin":
            raise approval_service.ApprovalError("Only finance administrators can process payments")
        if conn.execute("SELECT 1 FROM expenses WHERE id = ? AND org_id = ?", (expense_id, user["org_id"])).fetchone() is None:
            raise approval_service.ApprovalError("Expense not found", 404)

    @staticmethod
    def _log_orch(conn, org_id, expense_id, action, details):
        conn.execute("INSERT INTO agent_execution_logs (org_id, expense_id, agent_name, status, output_json)"
                     " VALUES (?,?,?,?,?)", (org_id, expense_id, "orchestrator", "SUCCESS",
                                             json.dumps({"event": action, **(details if isinstance(details, dict) else {})}, default=str)))

    # ------------------------------------------------------------------ monitoring
    def agent_status(self, conn, org_id: int) -> list:
        out = []
        descriptions = {n: a.description for n, a in self.agents.items()}
        descriptions["payment_agent"] = self.payment.description
        for name in ALL_AGENTS:
            if name == "payment_agent":
                row = conn.execute(
                    "SELECT COUNT(*) n, MAX(created_at) last FROM audit_logs WHERE org_id = ? AND action IN"
                    " ('PAYMENT_PREPARED','PAYOUT_SUBMITTED','PAYMENT_VERIFIED','PAYMENT_DEMO_ONLY')", (org_id,)).fetchone()
                errs = conn.execute("SELECT COUNT(*) c FROM audit_logs WHERE org_id = ? AND action = 'PAYMENT_FAILED'", (org_id,)).fetchone()["c"]
                out.append({"name": name, "description": descriptions[name], "runs": row["n"], "errors": errs, "last_run": row["last"],
                            "state": "error" if errs else "active" if row["n"] else "idle", "mode": self.payment.mode})
                continue
            row = conn.execute("SELECT COUNT(*) n, SUM(status='ERROR') e, MAX(created_at) last, AVG(duration_ms) avg_ms"
                               " FROM agent_execution_logs WHERE org_id = ? AND agent_name = ?", (org_id, name)).fetchone()
            last = conn.execute("SELECT status FROM agent_execution_logs WHERE org_id = ? AND agent_name = ?"
                                " ORDER BY id DESC LIMIT 1", (org_id, name)).fetchone()
            state = "idle" if not row["n"] else "error" if last and last["status"] == "ERROR" else "active"
            out.append({"name": name, "description": descriptions[name], "runs": row["n"], "errors": row["e"] or 0,
                        "last_run": row["last"], "avg_ms": round(row["avg_ms"] or 0), "state": state})
        return out


def _public(out: dict) -> dict:
    return {k: v for k, v in out.items() if not k.startswith("_")}


def init_orchestrator(app):
    """Called from create_app: warms spaCy and builds the orchestrator with the configured PayPal client."""
    from intelligence.nlp_engine import nlp_status
    status = nlp_status()
    app.logger.info("spaCy ready: %s", status)
    app.extensions["orchestrator"] = Orchestrator(client_from_config(app.config))
    app.extensions["nlp_status"] = status


def get_orchestrator() -> Orchestrator:
    return current_app.extensions["orchestrator"]
