"""Agent 2 - Policy Compliance: deterministic limit checks with explanations."""
from agents.base import BaseAgent, WorkflowState
from intelligence.policy_engine import evaluate_expense


class PolicyComplianceAgent(BaseAgent):
    name = "policy_agent"
    description = "Checks expenses against company policy limits and prohibited categories"

    def run(self, conn, state: WorkflowState) -> dict:
        exp = state.expense
        row = conn.execute(
            "SELECT category, limit_amount, prohibited FROM expense_policies WHERE org_id = ? AND category = ?",
            (state.org_id, exp["category"])).fetchone()
        verdict = evaluate_expense(dict(row) if row else None, exp["category"], exp["amount"], exp["currency"])
        verdict["requires_manager_attention"] = verdict["status"] != "COMPLIANT"
        return verdict
