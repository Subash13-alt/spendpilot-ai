"""Row-level visibility rules shared by routes and agents (organisation isolation first)."""

# Statuses that represent real or committed spend. Drafts and rejections are excluded.
SPEND_EXCLUDED = ("DRAFT", "REJECTED")
SPEND_SQL = "e.status NOT IN ('DRAFT','REJECTED')"


def scope_clause(user, alias: str = "e"):
    """SQL fragment + params limiting expenses to what this user may see."""
    clause, params = f"{alias}.org_id = ?", [user["org_id"]]
    if user["role"] == "employee":
        clause += f" AND {alias}.user_id = ?"
        params.append(user["id"])
    elif user["role"] == "manager":
        clause += f" AND {alias}.department_id = ?"
        params.append(user["department_id"])
    return clause, params


def get_visible_expense(conn, user, expense_id: int):
    """Return the expense row only if this user may see it (org isolation + role scope), else None."""
    where, params = scope_clause(user)
    return conn.execute(f"SELECT e.* FROM expenses e WHERE e.id = ? AND {where}", [expense_id] + params).fetchone()
