"""Simple, transparent spend forecasting (linear trend over monthly totals)."""
from datetime import date
from typing import List

import numpy as np

from services.scope import SPEND_SQL


def monthly_totals(conn, org_id: int, department_id: int = None) -> List[dict]:
    sql = (f"SELECT substr(e.expense_date,1,7) AS month, SUM(e.amount) AS total FROM expenses e"
           f" WHERE e.org_id = ? AND {SPEND_SQL}")
    params = [org_id]
    if department_id:
        sql += " AND e.department_id = ?"
        params.append(department_id)
    sql += " GROUP BY month ORDER BY month"
    return [{"month": r["month"], "total": round(r["total"], 2)} for r in conn.execute(sql, params)]


def _next_months(last: str, n: int) -> List[str]:
    y, m = int(last[:4]), int(last[5:7])
    out = []
    for _ in range(n):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        out.append(f"{y:04d}-{m:02d}")
    return out


def forecast(history: List[dict], horizon: int = 3) -> dict:
    """Least-squares trend on monthly totals. Honest about low data: <3 months uses the mean."""
    if not history:
        return {"history": [], "forecast": [], "method": "none", "confidence": "none",
                "note": "No spending history to forecast from."}
    values = np.array([h["total"] for h in history], dtype=float)
    months = _next_months(history[-1]["month"], horizon)
    if len(values) >= 3:
        x = np.arange(len(values))
        slope, intercept = np.polyfit(x, values, 1)
        preds = [max(0.0, float(intercept + slope * (len(values) + i))) for i in range(horizon)]
        method = "linear-trend"
        confidence = "medium" if len(values) >= 6 else "low"
    else:
        preds = [float(values.mean())] * horizon
        method, confidence = "mean", "low"
    return {"history": history, "forecast": [{"month": m, "total": round(p, 2)} for m, p in zip(months, preds)],
            "method": method, "confidence": confidence,
            "note": f"{method} over {len(values)} month(s) of history; treat as an estimate."}
