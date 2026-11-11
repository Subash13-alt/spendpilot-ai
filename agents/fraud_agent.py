"""Agent 3 - Fraud Detection: duplicate checks, history comparison, Isolation Forest.

Output is a *risk indicator for human review*. It never states that anyone committed fraud.
"""
import re
from datetime import date, datetime, timedelta

from agents.base import BaseAgent, WorkflowState
from intelligence.anomaly_detector import isolation_forest_score, zscore_check


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class FraudDetectionAgent(BaseAgent):
    name = "fraud_agent"
    description = "Duplicate receipts, repeated transactions and anomalous spending"

    def run(self, conn, state: WorkflowState) -> dict:
        exp, org = state.expense, state.org_id
        eid = state.expense_id
        signals = []  # (points, reason)

        # 1. Same receipt file submitted before
        if exp.get("receipt_hash"):
            dup = conn.execute(
                "SELECT id, user_id FROM expenses WHERE org_id = ? AND receipt_hash = ? AND id != ?"
                " AND status != 'DRAFT' ORDER BY id LIMIT 1", (org, exp["receipt_hash"], eid)).fetchone()
            if dup:
                same = dup["user_id"] == exp["user_id"]
                signals.append((85 if same else 90, f"Identical receipt file already submitted in expense #{dup['id']}"
                                                    + ("" if same else " by another employee")))

        # 2. Same merchant, date and amount (different file or no file)
        cands = conn.execute(
            "SELECT id, merchant FROM expenses WHERE org_id = ? AND id != ? AND expense_date = ? AND amount = ?"
            " AND status != 'DRAFT'", (org, eid, exp["expense_date"], exp["amount"])).fetchall()
        same_tx = [c for c in cands if _norm(c["merchant"]) == _norm(exp["merchant"])]
        if same_tx and not any("Identical receipt" in r for _, r in signals):
            signals.append((70, f"Possible duplicate transaction: same merchant, date and amount as expense #{same_tx[0]['id']}"))

        # 3. Repeated transactions at one merchant within 3 days
        try:
            d0 = datetime.strptime(exp["expense_date"], "%Y-%m-%d").date()
        except ValueError:
            d0 = None
        if d0:
            lo, hi = (d0 - timedelta(days=3)).isoformat(), (d0 + timedelta(days=3)).isoformat()
            near = conn.execute(
                "SELECT merchant FROM expenses WHERE org_id = ? AND user_id = ? AND id != ? AND status != 'DRAFT'"
                " AND expense_date BETWEEN ? AND ?", (org, exp["user_id"], eid, lo, hi)).fetchall()
            repeats = sum(1 for r in near if _norm(r["merchant"]) == _norm(exp["merchant"]))
            if repeats >= 2:
                signals.append((30, f"Repeated spending at this merchant ({repeats + 1} claims within a week)"))
            if d0 > date.today() + timedelta(days=1):
                signals.append((25, "Expense date is in the future"))
            elif d0 < date.today() - timedelta(days=90):
                signals.append((10, "Expense is more than 90 days old"))

        # 4. History comparison (employee, then organisation, in the same category)
        hist = conn.execute(
            "SELECT amount FROM expenses WHERE org_id = ? AND user_id = ? AND category = ? AND id != ?"
            " AND status NOT IN ('DRAFT','REJECTED')", (org, exp["user_id"], exp["category"], eid)).fetchall()
        z = zscore_check(exp["amount"], [h["amount"] for h in hist])
        scope = "this employee's"
        if not z["available"]:
            hist = conn.execute(
                "SELECT amount FROM expenses WHERE org_id = ? AND category = ? AND id != ?"
                " AND status NOT IN ('DRAFT','REJECTED')", (org, exp["category"], eid)).fetchall()
            z = zscore_check(exp["amount"], [h["amount"] for h in hist])
            scope = "the organisation's"
        if z["available"] and z["score"] > 0:
            signals.append((round(40 * z["score"]),
                            f"Amount is {z['z']:.1f} robust deviations above {scope} typical {exp['category']} "
                            f"spend (median ${z['median']:,.2f})"))

        # 5. Isolation Forest on organisation history
        rows = conn.execute(
            "SELECT amount, category, expense_date FROM expenses WHERE org_id = ? AND id != ?"
            " AND status NOT IN ('DRAFT','REJECTED')", (org, eid)).fetchall()
        hist_rows = [{"amount": r["amount"], "category": r["category"], "weekday": _weekday(r["expense_date"])} for r in rows]
        cand = {"amount": exp["amount"], "category": exp["category"], "weekday": _weekday(exp["expense_date"])}
        iso = isolation_forest_score(cand, hist_rows)
        if iso["available"] and iso["score"] > 0.3:
            signals.append((round(30 * iso["score"]), "Isolation Forest rates this expense as unusual versus organisation history"))

        # 6. Hygiene
        if not exp.get("receipt_path"):
            signals.append((10, "No receipt attached"))
        if exp["amount"] >= 500 and float(exp["amount"]).is_integer():
            signals.append((5, "Large round-number amount"))

        score = min(100, sum(p for p, _ in signals))
        level = "HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW"
        reasons = [r for _, r in sorted(signals, key=lambda s: -s[0])]
        recommendation = {
            "HIGH": "Manual investigation required before any approval",
            "MEDIUM": "Reviewer should verify the receipt and merchant details",
            "LOW": "No additional action needed",
        }[level]
        return {
            "risk_level": level, "risk_score": score,
            "reason": "; ".join(reasons) if reasons else "No risk indicators found",
            "indicators": reasons, "recommendation": recommendation,
            "methods": {"zscore": z, "isolation_forest": iso},
            "disclaimer": "Risk indicators are statistical signals for human review, not a finding of misconduct.",
            "explanation": (f"Risk {level} ({score}/100). " + ("; ".join(reasons) if reasons else "Nothing unusual detected.")),
        }


def _weekday(s: str) -> int:
    try:
        return datetime.strptime(s, "%Y-%m-%d").weekday()
    except (ValueError, TypeError):
        return 0
