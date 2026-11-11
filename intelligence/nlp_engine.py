"""Shared spaCy pipeline with receipt-specific EntityRuler patterns.

spaCy's general English model does not reliably tag receipt amounts or "Total" lines,
so a custom EntityRuler is added in front of the statistical NER component.
"""
import logging
import threading

import spacy

log = logging.getLogger(__name__)

MODEL_NAME = "en_core_web_sm"
CURRENCY_CODES = ["USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY"]
SYMBOLS = ["$", "€", "£", "₹"]
AMOUNT_RE = r"^\d[\d,]*(\.\d{1,2})?$"

_lock = threading.Lock()
_nlp = None
_status = {"model": None, "fallback": False}


def _patterns():
    amt = {"TEXT": {"REGEX": AMOUNT_RE}}
    return [
        # monetary amounts: "$350.00", "USD 350.00", "350.00 USD"
        {"label": "MONEY_AMT", "pattern": [{"TEXT": {"IN": SYMBOLS}}, amt]},
        {"label": "MONEY_AMT", "pattern": [{"TEXT": {"IN": CURRENCY_CODES}}, amt]},
        {"label": "MONEY_AMT", "pattern": [{"TEXT": {"REGEX": r"^\d[\d,]*\.\d{2}$"}}, {"TEXT": {"IN": CURRENCY_CODES}}]},
        {"label": "MONEY_AMT", "pattern": [{"TEXT": {"REGEX": r"^\d[\d,]*\.\d{2}$"}}]},
        # labels that mark the payable total
        {"label": "TOTAL_LABEL", "pattern": [{"LOWER": "grand"}, {"LOWER": "total"}]},
        {"label": "TOTAL_LABEL", "pattern": [{"LOWER": "total"}, {"LOWER": {"IN": ["due", "amount", "paid", "payable"]}}]},
        {"label": "TOTAL_LABEL", "pattern": [{"LOWER": {"IN": ["amount", "balance"]}}, {"LOWER": {"IN": ["due", "paid", "payable"]}}]},
        {"label": "TOTAL_LABEL", "pattern": [{"LOWER": "total"}]},
        # labels that mark amounts which are NOT the payable total
        {"label": "NON_TOTAL_LABEL", "pattern": [{"LOWER": "sub"}, {"ORTH": "-"}, {"LOWER": "total"}]},
        {"label": "NON_TOTAL_LABEL", "pattern": [{"LOWER": {"IN": [
            "subtotal", "tax", "vat", "gst", "tip", "gratuity", "change", "cash", "tendered", "discount", "deposit"]}}]},
        # currency markers
        {"label": "CURRENCY_MARK", "pattern": [{"TEXT": {"IN": CURRENCY_CODES}}]},
        {"label": "CURRENCY_MARK", "pattern": [{"TEXT": {"IN": SYMBOLS}}]},
    ]


def _build():
    try:
        nlp = spacy.load(MODEL_NAME)
        _status.update(model=MODEL_NAME, fallback=False)
    except OSError:
        log.warning("spaCy model %s missing (run: python -m spacy download %s). "
                    "Using blank English pipeline; receipt rules still work, generic NER is disabled.",
                    MODEL_NAME, MODEL_NAME)
        nlp = spacy.blank("en")
        nlp.add_pipe("sentencizer")
        _status.update(model="blank-en", fallback=True)
    kwargs = {"before": "ner"} if "ner" in nlp.pipe_names else {}
    ruler = nlp.add_pipe("entity_ruler", config={"overwrite_ents": True}, **kwargs)
    ruler.add_patterns(_patterns())
    return nlp


def get_nlp():
    """Lazily build (once) and return the shared pipeline."""
    global _nlp
    if _nlp is None:
        with _lock:
            if _nlp is None:
                _nlp = _build()
    return _nlp


def nlp_status() -> dict:
    get_nlp()
    return dict(_status, pipeline=list(get_nlp().pipe_names))
