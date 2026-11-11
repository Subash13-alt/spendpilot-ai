"""Agent 5 - Financial Intelligence: budgets, reports, forecasts and a controlled natural-language query engine.

The query engine NEVER builds SQL from user text. spaCy interprets the question into an intent plus
typed parameters (a department id, a number); each intent maps to a fixed, parameterised query.
"""
import re
from datetime import date
from typing import Optional

from agents.base import BaseAgent, WorkflowState
from intelligence.forecasting import forecast, monthly_totals
from intelligence.nlp_engine import get_nlp
from services.scope import SPEND_SQL, scope_clause

WARN_PCT = 80.0
DEPT_ALIASES = {"hr": "human resources", "eng": "engineering", "ops": "operations", "mkt": "marketing"}


def current_period() -> str:
    return str(date.today().year)


class FinancialIntelligenceAgent(BaseAgent):
    name = "finance_agent"
    description = "Budget monitoring, spend analytics, forecasting and natural-language financial queries"

    # ------------------------------------------------------------------ budgets
    def budget_status(self, conn, org_id: int, department_id: Optional[int] = None, period: Optional[str] = None):
        period = period or current_period()
        sql = ("SELECT d.id, d.name, COALESCE(b.amount, 0) AS budget,"
               " COALESCE((SELECT SUM(e.amount) FROM expenses e WHERE e.department_id = d.id"
               f"   AND {SPEND_SQL} AND substr(e.expense_date,1,4) = ?), 0) AS spent"
               " FROM departments d LEFT JOIN budgets b ON b.department_id = d.id AND b.period = ?"
               " WHERE d.org_id = ?")
        params = [period, period, org_id]
        if department_id:
            sql += " AND d.id = ?"
            params.append(department_id)
        out = []
        for r in conn.execute(sql + " ORDER BY d.name", params):
            budget, spent = float(r["budget"]), float(r["spent"])
            pct = round(spent / budget * 100, 1) if budget else None
            level = "NO_BUDGET" if not budget else "EXCEEDED" if spent > budget else "WARNING" if pct >= WARN_PCT else "OK"
            out.append({"department_id": r["id"], "department": r["name"], "period": period,
                        "budget": budget, "spent": round(spent, 2), "remaining": round(budget - spent, 2),
                        "utilization_pct": pct, "level": level})
        return out

    def run(self, conn, state: WorkflowState) -> dict:
        """Budget impact of the expense under analysis (the expense is already counted in 'spent')."""
        exp = state.expense
        rows = self.budget_status(conn, state.org_id, exp.get("department_id"), (exp["expense_date"] or "")[:4] or None)
        b = rows[0] if rows else None
        if not b or b["level"] == "NO_BUDGET":
            return {"budget_level": "NO_BUDGET", "explanation": "No budget is defined for this department and period."}
        msg = {"OK": "Department budget is healthy.",
               "WARNING": f"Department has used {b['utilization_pct']}% of its {b['period']} budget.",
               "EXCEEDED": f"Department is over its {b['period']} budget by ${-b['remaining']:,.2f}."}[b["level"]]
        return {"budget_level": b["level"], "department": b["department"], "budget": b["budget"],
                "spent": b["spent"], "remaining": b["remaining"], "utilization_pct": b["utilization_pct"],
                "explanation": msg}

    # ------------------------------------------------------------------ reports
    def summary(self, conn, user) -> dict:
        where, params = scope_clause(user)
        r = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN {SPEND_SQL} THEN amount END),0) spend,"
            " COALESCE(SUM(CASE WHEN e.status LIKE 'PENDING%' OR e.status='FLAGGED' THEN amount END),0) pending_amt,"
            " COALESCE(SUM(CASE WHEN e.status IN ('MANAGER_APPROVED','PENDING_FINANCE_AUTHORIZATION',"
            "   'FINANCE_AUTHORIZED','PAYMENT_PROCESSING','PAID') THEN amount END),0) approved_amt,"
            " COALESCE(SUM(CASE WHEN e.status='REJECTED' THEN amount END),0) rejected_amt,"
            " COALESCE(SUM(CASE WHEN e.status IN ('FINANCE_AUTHORIZED','PAYMENT_PROCESSING','PAYMENT_FAILED')"
            "   THEN amount END),0) reimb_pending_amt,"
            " COALESCE(SUM(CASE WHEN e.status='PAID' THEN amount END),0) paid_amt"
            f" FROM expenses e WHERE {where}", params).fetchone()
        alerts = conn.execute(
            "SELECT COUNT(*) c FROM fraud_alerts f JOIN expenses e ON e.id = f.expense_id"
            f" WHERE f.status='OPEN' AND {where}", params).fetchone()["c"]
        d = dict(r)
        d["open_fraud_alerts"] = alerts
        return d

    def by_department(self, conn, user) -> list:
        where, params = scope_clause(user)
        rows = conn.execute(
            "SELECT d.name AS department, COALESCE(SUM(e.amount),0) AS total, COUNT(e.id) AS n"
            f" FROM departments d LEFT JOIN expenses e ON e.department_id = d.id AND {SPEND_SQL}"
            f" AND {where.replace('e.org_id', 'e.org_id')}"
            " WHERE d.org_id = ? GROUP BY d.id ORDER BY total DESC", params + [user["org_id"]])
        return [{"department": r["department"], "total": round(r["total"], 2), "count": r["n"]} for r in rows]

    def by_category(self, conn, user) -> list:
        where, params = scope_clause(user)
        rows = conn.execute(
            f"SELECT e.category, SUM(e.amount) total, COUNT(*) n FROM expenses e WHERE {where} AND {SPEND_SQL}"
            " GROUP BY e.category ORDER BY total DESC", params)
        return [{"category": r["category"], "total": round(r["total"], 2), "count": r["n"]} for r in rows]

    def monthly_forecast(self, conn, user, horizon: int = 3) -> dict:
        dept = user["department_id"] if user["role"] == "manager" else None
        return forecast(monthly_totals(conn, user["org_id"], dept), horizon)

    # ------------------------------------------------------------------ NL query engine
    def answer_question(self, conn, user, question: str) -> dict:
        question = (question or "").strip()
        if not question or len(question) > 300:
            return {"ok": False, "answer": "Please ask a question of up to 300 characters."}
        intent, params = self._interpret(conn, user, question)
        if intent is None:
            return {"ok": False, "intent": None, "answer": (
                "I can answer questions such as: total spend for a department, which department spends the most, "
                "expenses above/below an amount, remaining budget, pending reimbursements, or open fraud alerts.")}
        if user["role"] == "employee" and intent not in ("expenses_above", "expenses_below", "total_spend"):
            return {"ok": False, "intent": intent, "answer": "Employees can only query their own expenses."}
        handler = getattr(self, f"_q_{intent}")
        res = handler(conn, user, **params)
        res.update(ok=True, intent=intent, params={k: v for k, v in params.items()})
        return res

    def _interpret(self, conn, user, question: str):
        doc = get_nlp()(question.lower())
        lemmas = {t.lemma_ for t in doc} | {t.lower_ for t in doc}
        text = question.lower()
        dept = self._find_department(conn, user["org_id"], text)
        amount = self._find_amount(text)
        has = lambda *w: any(x in lemmas or x in text for x in w)

        if has("reimbursement", "reimburse") and has("pending", "outstanding", "unpaid", "owed", "waiting"):
            return "pending_reimbursements", {}
        if has("fraud", "suspicious", "anomaly", "alert"):
            return "open_alerts", {}
        if has("budget") and has("remain", "left", "remaining", "available"):
            return "budget_remaining", {"department_id": dept[0] if dept else None}
        if amount is not None and has("expense", "spend", "claim", "transaction", "show", "list"):
            if has("below", "under", "less", "lower", "cheaper"):
                return "expenses_below", {"amount": amount}
            if has("above", "over", "more", "greater", "exceed", "higher", "beyond"):
                return "expenses_above", {"amount": amount}
        if has("highest", "most", "largest", "biggest", "top") and has("department", "team", "spend", "spending", "expenditure"):
            return "top_department", {}
        if dept and has("total", "spend", "spending", "expenditure", "expense", "cost", "how much"):
            return "department_total", {"department_id": dept[0], "department_name": dept[1]}
        if has("pending") and has("approval", "approve", "expense"):
            return "pending_approvals", {}
        if has("total") and has("spend", "spending", "expenditure", "expense", "cost"):
            return "total_spend", {}
        return None, {}

    @staticmethod
    def _find_amount(text: str) -> Optional[float]:
        m = re.search(r"[$€£₹]\s*(\d[\d,]*(?:\.\d+)?)|\b(\d[\d,]*(?:\.\d+)?)\s*(?:usd|dollars)?\b", text)
        if not m:
            return None
        try:
            return float((m.group(1) or m.group(2)).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _find_department(conn, org_id: int, text: str):
        words = set(re.findall(r"[a-z]+", text))
        for alias, full in DEPT_ALIASES.items():
            if alias in words:
                text += " " + full
        for r in conn.execute("SELECT id, name FROM departments WHERE org_id = ?", (org_id,)):
            if r["name"].lower() in text:
                return r["id"], r["name"]
        return None

    # each handler returns {answer, columns, rows}; all SQL is fixed and parameterised
    def _q_total_spend(self, conn, user):
        where, params = scope_clause(user)
        total = conn.execute(f"SELECT COALESCE(SUM(amount),0) t FROM expenses e WHERE {where} AND {SPEND_SQL}", params).fetchone()["t"]
        return {"answer": f"Total expenditure is ${total:,.2f}.", "columns": [], "rows": []}

    def _q_department_total(self, conn, user, department_id, department_name):
        where, params = scope_clause(user)
        r = conn.execute(f"SELECT COALESCE(SUM(amount),0) t, COUNT(*) n FROM expenses e WHERE {where}"
                         f" AND {SPEND_SQL} AND e.department_id = ?", params + [department_id]).fetchone()
        return {"answer": f"{department_name} expenditure is ${r['t']:,.2f} across {r['n']} expense(s).",
                "columns": [], "rows": []}

    def _q_top_department(self, conn, user):
        rows = self.by_department(conn, user)
        if not rows or rows[0]["total"] == 0:
            return {"answer": "No spending recorded yet.", "columns": [], "rows": []}
        top = rows[0]
        return {"answer": f"{top['department']} has the highest spending at ${top['total']:,.2f}.",
                "columns": ["Department", "Total", "Expenses"],
                "rows": [[r["department"], f"${r['total']:,.2f}", r["count"]] for r in rows]}

    def _expenses_cmp(self, conn, user, amount, op):
        where, params = scope_clause(user)
        sql = ("SELECT e.id, e.merchant, e.category, e.amount, e.status, u.name AS employee FROM expenses e"
               f" JOIN users u ON u.id = e.user_id WHERE {where} AND {SPEND_SQL} AND e.amount {op} ?"
               " ORDER BY e.amount DESC LIMIT 50")
        rows = conn.execute(sql, params + [amount]).fetchall()
        word = "above" if op == ">" else "below"
        return {"answer": f"{len(rows)} expense(s) {word} ${amount:,.2f}.",
                "columns": ["ID", "Merchant", "Category", "Amount", "Status", "Employee"],
                "rows": [[r["id"], r["merchant"], r["category"], f"${r['amount']:,.2f}", r["status"], r["employee"]] for r in rows]}

    def _q_expenses_above(self, conn, user, amount):
        return self._expenses_cmp(conn, user, amount, ">")

    def _q_expenses_below(self, conn, user, amount):
        return self._expenses_cmp(conn, user, amount, "<")

    def _q_budget_remaining(self, conn, user, department_id=None):
        if user["role"] == "manager":
            department_id = user["department_id"]
        rows = self.budget_status(conn, user["org_id"], department_id)
        tot_b, tot_r = sum(r["budget"] for r in rows), sum(r["remaining"] for r in rows)
        label = rows[0]["department"] if department_id and rows else "all departments"
        return {"answer": f"Remaining budget for {label}: ${tot_r:,.2f} of ${tot_b:,.2f}.",
                "columns": ["Department", "Budget", "Spent", "Remaining", "Used %"],
                "rows": [[r["department"], f"${r['budget']:,.2f}", f"${r['spent']:,.2f}", f"${r['remaining']:,.2f}",
                          r["utilization_pct"] if r["utilization_pct"] is not None else "-"] for r in rows]}

    def _q_pending_reimbursements(self, conn, user):
        where, params = scope_clause(user)
        rows = conn.execute(
            "SELECT u.name, COUNT(*) n, SUM(e.amount) t FROM expenses e JOIN users u ON u.id = e.user_id"
            f" WHERE {where} AND e.status IN ('MANAGER_APPROVED','PENDING_FINANCE_AUTHORIZATION',"
            "'FINANCE_AUTHORIZED','PAYMENT_PROCESSING','PAYMENT_FAILED') GROUP BY u.id ORDER BY t DESC", params).fetchall()
        return {"answer": f"{len(rows)} employee(s) have pending reimbursements.",
                "columns": ["Employee", "Expenses", "Amount"],
                "rows": [[r["name"], r["n"], f"${r['t']:,.2f}"] for r in rows]}

    def _q_pending_approvals(self, conn, user):
        where, params = scope_clause(user)
        n = conn.execute(f"SELECT COUNT(*) c FROM expenses e WHERE {where} AND e.status IN"
                         " ('PENDING_MANAGER_APPROVAL','FLAGGED','PENDING_FINANCE_AUTHORIZATION')", params).fetchone()["c"]
        return {"answer": f"{n} expense(s) are waiting for a human decision.", "columns": [], "rows": []}

    def _q_open_alerts(self, conn, user):
        where, params = scope_clause(user)
        rows = conn.execute(
            "SELECT f.expense_id, f.risk_level, f.risk_score, f.reason FROM fraud_alerts f JOIN expenses e ON e.id = f.expense_id"
            f" WHERE f.status = 'OPEN' AND {where} ORDER BY f.risk_score DESC", params).fetchall()
        return {"answer": f"{len(rows)} open risk alert(s) awaiting human review.",
                "columns": ["Expense", "Level", "Score", "Reason"],
                "rows": [[r["expense_id"], r["risk_level"], r["risk_score"], r["reason"]] for r in rows]}
