"""Receipt entity extraction: merchant, date, amount, currency.

Combines the custom spaCy EntityRuler (nlp_engine) with regular expressions and
validation rules. Every field carries a note when it was guessed, so the employee can correct it.
"""
import re
from datetime import date, datetime
from typing import Optional

from intelligence.nlp_engine import get_nlp

SYMBOL_TO_CODE = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
CODES = {"USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

MAX_REASONABLE_AMOUNT = 1_000_000
NOISE_LINE = re.compile(r"^(date|total|subtotal|tax|vat|gst|invoice|receipt|bill|order|tel|phone|www|http|thank|amount|"
                        r"cash|change|visa|mastercard|card|address|gstin|room|check)\b", re.I)
MERCHANT_HINT = re.compile(r"\b(hotel|inn|resort|restaurant|cafe|caf[eé]|grill|bistro|airlines?|airways|taxi|cabs?|"
                           r"ltd|llc|inc|corp|co\.|store|mart|market|suites?|lodge|diner|bar|pub)\b", re.I)

_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})\b")
_YMD = re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
_DMONY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4})\b")
_MONDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_AMOUNT = re.compile(r"(?P<sym>[$€£₹])\s*(?P<a>\d[\d,]*(?:\.\d{1,2})?)"
                     r"|(?P<code>USD|EUR|GBP|INR|CAD|AUD|JPY)\s*(?P<b>\d[\d,]*(?:\.\d{1,2})?)"
                     r"|(?P<c>\d[\d,]*\.\d{2})\s*(?P<code2>USD|EUR|GBP|INR|CAD|AUD|JPY)?", re.I)


def _to_float(s: str) -> Optional[float]:
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return round(v, 2)


def _make_date(y: int, m: int, d: int) -> Optional[date]:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_date(text: str) -> dict:
    """Return {value: 'YYYY-MM-DD'|None, note}. Numeric dates default to day-first (09/11/2026 -> 2026-11-09)."""
    m = _YMD.search(text)
    if m:
        d = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return {"value": d.isoformat(), "note": None}
    m = _DMONY.search(text)
    if m and m.group(2)[:3].lower() in MONTHS:
        d = _make_date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        if d:
            return {"value": d.isoformat(), "note": None}
    m = _MONDY.search(text)
    if m and m.group(1)[:3].lower() in MONTHS:
        d = _make_date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        if d:
            return {"value": d.isoformat(), "note": None}
    m = _DMY.search(text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if b > 12 and a <= 12:  # clearly month/day
            d, note = _make_date(y, a, b), None
        elif a > 12:  # clearly day/month
            d, note = _make_date(y, b, a), None
        else:
            d = _make_date(y, b, a)
            note = "Ambiguous numeric date read as DD/MM/YYYY - please confirm" if a != b else None
        if d:
            return {"value": d.isoformat(), "note": note}
    return {"value": None, "note": "No date found"}


def extract_currency(text: str) -> dict:
    m = re.search(r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY)\b", text, re.I)
    if m:
        return {"value": m.group(1).upper(), "note": None}
    for sym, code in SYMBOL_TO_CODE.items():
        if sym in text:
            return {"value": code, "note": None}
    if re.search(r"\bRs\.?\s*\d", text, re.I):
        return {"value": "INR", "note": None}
    return {"value": "USD", "note": "No currency marker found - assumed USD"}


def extract_amount(text: str) -> dict:
    """Pick the payable total: prefer amounts on 'Total' lines, ignore subtotal/tax/tip/change lines."""
    doc = get_nlp()(text)
    candidates = []  # (priority, position, amount)
    pos = 0
    for line in text.splitlines():
        low = line.lower()
        ldoc = get_nlp()(line)
        labels = {e.label_ for e in ldoc.ents}
        is_total = "TOTAL_LABEL" in labels or bool(re.search(r"\b(grand\s+total|total|amount\s+(due|paid)|balance\s+due)\b", low))
        non_total = "NON_TOTAL_LABEL" in labels or bool(re.search(r"\b(sub-?\s?total|tax|vat|gst|tip|change|cash|tendered|discount)\b", low))
        if re.search(r"\bsub-?\s?total\b", low):
            is_total = False
        for m in _AMOUNT.finditer(line):
            raw = m.group("a") or m.group("b") or m.group("c")
            val = _to_float(raw)
            if val is None or val <= 0:
                continue
            pos += 1
            if is_total and not non_total:
                prio = 3 if re.search(r"grand\s+total|amount\s+due|balance\s+due", low) else 2
            elif non_total:
                prio = 0
            else:
                prio = 1
            candidates.append((prio, pos, val))
    if not candidates:
        return {"value": None, "note": "No monetary amount found"}
    top = max(c[0] for c in candidates)
    pool = [c for c in candidates if c[0] == top]
    if top == 0:
        return {"value": None, "note": "Only tax/tip/change style amounts found - enter the total manually"}
    chosen = max(pool, key=lambda c: (c[2], c[1])) if top == 1 else pool[-1]
    val = chosen[2]
    if val > MAX_REASONABLE_AMOUNT:
        return {"value": None, "note": f"Amount {val:,.2f} exceeds sanity limit - please enter manually"}
    note = None if top >= 2 else "Total line not found - used the largest amount on the receipt"
    return {"value": val, "note": note}


def extract_merchant(text: str) -> dict:
    """Prefer 'Merchant:' labels, then spaCy ORG entities, then the first plausible header line."""
    m = re.search(r"(?im)^\s*(?:merchant|vendor|store|sold by|billed by)\s*[:\-]\s*(.+)$", text)
    if m:
        return {"value": _clean_name(m.group(1)), "note": None}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    header = [l for l in lines[:6] if not NOISE_LINE.match(l) and not re.fullmatch(r"[\d\W]+", l)]
    # Header segments split on '.', so "Hotel Grand Palace. Date: ..." still works on one line.
    for line in header:
        seg = re.split(r"\.\s+|\s{2,}|\|", line)[0]
        if MERCHANT_HINT.search(seg) and 2 <= len(seg) <= 60:
            return {"value": _clean_name(seg), "note": None}
    doc = get_nlp()("\n".join(lines[:8]))
    orgs = [e.text for e in doc.ents if e.label_ == "ORG" and not e.text.isupper() or e.label_ == "ORG"]
    if orgs:
        return {"value": _clean_name(orgs[0]), "note": "Merchant inferred by NER - please verify"}
    if header:
        seg = re.split(r"\.\s+|\s{2,}|\|", header[0])[0]
        return {"value": _clean_name(seg)[:60], "note": "Merchant guessed from first line - please verify"}
    return {"value": None, "note": "No merchant found"}


def _clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" .:-*#")


def extract_receipt_entities(text: str) -> dict:
    """Run all extractors. Returns field values plus per-field notes and an overall confidence."""
    text = (text or "").strip()
    if not text:
        return {"merchant": None, "date": None, "amount": None, "currency": "USD", "notes": ["Receipt text is empty"],
                "confidence": 0.0}
    merchant, dt, amt, cur = extract_merchant(text), extract_date(text), extract_amount(text), extract_currency(text)
    notes = [n for n in (merchant["note"], dt["note"], amt["note"], cur["note"]) if n]
    found = sum(v["value"] is not None for v in (merchant, dt, amt))
    penalty = 0.07 * len(notes)
    confidence = round(max(0.0, min(0.98, found / 3 * 0.95 - penalty)), 2)
    return {"merchant": merchant["value"], "date": dt["value"], "amount": amt["value"],
            "currency": cur["value"], "notes": notes, "confidence": confidence}
