"""Agent 4 - Approval Intelligence: routing and a *recommendation* for the human approver.

This agent never approves, rejects or authorises anything. It only advises.
"""
from agents.base import BaseAgent, WorkflowState

SENIOR_REVIEW_THRESHOLD = 1000.0  # expenses above this are marked for extra scrutiny at finance stage


class ApprovalIntelligenceAgent(BaseAgent):
    name = "approval_agent"
    description = "Determines the approval route and recommends a decision to human approvers"

    def run(self, conn, state: WorkflowState) -> dict:
        exp = state.expense
        policy = state.results.get("policy_agent", {})
        fraud = state.results.get("fraud_agent", {})
        expense_ai = state.results.get("expense_agent", {})
        budget = state.results.get("finance_agent", {})

        dept = conn.execute("SELECT manager_id FROM departments WHERE id = ?", (exp.get("department_id"),)).fetchone()
        manager_id = dept["manager_id"] if dept else None
        route = ["Manager review", "Finance administrator review", "Payment authorization"]
        concerns, blockers = [], []

        if policy.get("status") == "PROHIBITED":
            blockers.append(policy["explanation"])
        elif policy.get("status") == "VIOLATION":
            concerns.append(f"Policy: ${policy['excess_amount']:,.2f} over the ${policy['limit']:,.2f} limit")
        elif policy.get("status") in ("NO_POLICY", "REVIEW"):
            concerns.append(f"Policy: {policy['explanation']}")
        if fraud.get("risk_level") == "HIGH":
            blockers.append(f"High risk indicators ({fraud['risk_score']}/100): {fraud['reason']}")
        elif fraud.get("risk_level") == "MEDIUM":
            concerns.append(f"Medium risk indicators ({fraud['risk_score']}/100): {fraud['reason']}")
        if expense_ai.get("category_mismatch"):
            concerns.append("Category may not match the receipt text")
        if not expense_ai.get("has_receipt", True):
            concerns.append("No receipt attached")
        if budget.get("budget_level") == "EXCEEDED":
            concerns.append(budget["explanation"])
        elif budget.get("budget_level") == "WARNING":
            concerns.append(budget["explanation"])
        extra_review = exp["amount"] > SENIOR_REVIEW_THRESHOLD
        if extra_review:
            concerns.append(f"Amount above ${SENIOR_REVIEW_THRESHOLD:,.0f}: extra scrutiny at finance stage")

        if blockers:
            rec, label = "REJECT_OR_INVESTIGATE", "Recommend rejection or investigation"
        elif concerns:
            rec, label = "REVIEW", "Recommend careful manual review"
        else:
            rec, label = "APPROVE", "Recommend approval"
        reasons = blockers + concerns
        return {
            "recommendation": rec, "recommendation_label": label,
            "route": route, "assigned_manager_id": manager_id,
            "requires_additional_review": bool(blockers or concerns), "extra_finance_scrutiny": extra_review,
            "reasons": reasons, "human_decision_required": True,
            "explanation": label + (": " + "; ".join(reasons) if reasons else ": no policy, risk or budget concerns found")
                           + ". This is advice only - a human must decide.",
        }

    @staticmethod
    def history(conn, expense_id: int) -> list:
        return [dict(r) for r in conn.execute(
            "SELECT a.stage, a.decision, a.comment, a.created_at, u.name AS approver FROM approvals a"
            " JOIN users u ON u.id = a.approver_id WHERE a.expense_id = ? ORDER BY a.id", (expense_id,))]
