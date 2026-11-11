"""Deterministic policy evaluation plus spaCy-assisted parsing of natural-language policy text.

All money maths is plain Python (Decimal) - no model is involved in a compliance decision.
"""
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from intelligence.expense_classifier import CATEGORY_KEYWORDS
from intelligence.nlp_engine import get_nlp

POLICY_CURRENCY = "USD"
_NEG = {"not", "never", "prohibited", "banned", "forbidden", "ineligible", "non-reimbursable", "nonreimbursable"}


def _d(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_policy_text(text: str) -> dict:
    """Interpret 'Hotel stays are limited to $200 per night' -> {category, limit_amount, prohibited}.

    Uses spaCy tokens/lemmas to find the category and to detect prohibition wording,
    and the EntityRuler's money patterns (via regex fallback) for the limit.
    """
    doc = get_nlp()(text)
    lemmas = {t.lemma_.lower() for t in doc} | {t.lower_ for t in doc}
    lemmas |= {w[:-1] for w in lemmas if w.endswith("s") and len(w) > 3}
    category, best = None, 0
    for cat, words in CATEGORY_KEYWORDS.items():
        score = sum(wt for w, wt in words.items() if w in lemmas)
        if cat.lower() in lemmas:
            score += 5
        if score > best:
            category, best = cat, score
    prohibited = bool(lemmas & _NEG) or bool(re.search(r"\bnot\s+(?:be\s+)?reimburs", text, re.I))
    m = re.search(r"[$€£₹]\s*(\d[\d,]*(?:\.\d{1,2})?)|\b(\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD|dollars)\b", text, re.I)
    limit = None
    if m:
        limit = float((m.group(1) or m.group(2)).replace(",", ""))
    if category is None:
        raise ValueError("Could not identify an expense category in the policy text")
    if not prohibited and limit is None:
        raise ValueError("Could not find a spending limit in the policy text")
    return {"category": category, "limit_amount": None if prohibited else limit, "prohibited": prohibited}


def evaluate_expense(policy: Optional[dict], category: str, amount: float, currency: str = "USD") -> dict:
    """Compare one expense with its category policy. Returns the structured compliance verdict."""
    actual = _d(amount)
    base = {"category": category, "actual_amount": float(actual), "currency": currency}
    if policy is None:
        return {**base, "status": "NO_POLICY", "limit": None, "excess_amount": 0.0,
                "explanation": f"No policy is defined for '{category}', so a manager must review it.",
                "recommendation": "Manager review required"}
    if policy["prohibited"]:
        return {**base, "status": "PROHIBITED", "limit": 0, "excess_amount": float(actual),
                "explanation": f"'{category}' is a prohibited expense category under company policy.",
                "recommendation": "Reject unless a documented exception applies"}
    limit = _d(policy["limit_amount"])
    if currency != POLICY_CURRENCY:
        return {**base, "status": "REVIEW", "limit": float(limit), "excess_amount": 0.0,
                "explanation": f"Policy limits are in {POLICY_CURRENCY}; this expense is in {currency} and no "
                               "exchange-rate source is configured, so it cannot be checked automatically.",
                "recommendation": "Manager review required (currency conversion)"}
    if actual > limit:
        excess = actual - limit
        return {**base, "status": "VIOLATION", "limit": float(limit), "excess_amount": float(excess),
                "explanation": f"{category} limit is ${limit:,.2f}; the expense is ${actual:,.2f}, "
                               f"which is ${excess:,.2f} over the limit.",
                "recommendation": "Manager review required"}
    return {**base, "status": "COMPLIANT", "limit": float(limit), "excess_amount": 0.0,
            "explanation": f"${actual:,.2f} is within the ${limit:,.2f} {category} limit.",
            "recommendation": "No policy action needed"}
