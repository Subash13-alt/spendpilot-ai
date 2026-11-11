"""Keyword-weighted expense category classifier driven by spaCy lemmas."""
import re

from intelligence.nlp_engine import get_nlp

CATEGORY_KEYWORDS = {
    "Accommodation": {"hotel": 3, "inn": 2, "resort": 3, "lodge": 2, "motel": 3, "hostel": 3, "suite": 2,
                      "airbnb": 3, "accommodation": 3, "room": 1, "night": 1, "stay": 1, "checkout": 1, "lodging": 3},
    "Food": {"restaurant": 3, "cafe": 3, "coffee": 2, "diner": 3, "pizza": 3, "burger": 3, "lunch": 2,
             "dinner": 2, "breakfast": 2, "meal": 2, "bistro": 3, "grill": 2, "kitchen": 1, "bakery": 3,
             "catering": 3, "food": 2, "sandwich": 2, "starbucks": 3},
    "Transportation": {"taxi": 3, "uber": 3, "lyft": 3, "cab": 3, "flight": 3, "airline": 3, "airways": 3,
                       "train": 3, "rail": 2, "fuel": 3, "petrol": 3, "gas": 1, "parking": 3, "toll": 3,
                       "metro": 3, "bus": 2, "ride": 1, "rental": 2, "ticket": 1},
    "Office Supplies": {"stationery": 3, "staples": 3, "office": 2, "paper": 2, "printer": 3, "toner": 3,
                        "ink": 2, "supply": 2, "desk": 1, "pen": 1, "notebook": 2},
    "Software": {"software": 3, "subscription": 3, "license": 3, "licence": 3, "saas": 3, "cloud": 2,
                 "hosting": 3, "github": 3, "aws": 3, "azure": 3, "domain": 2, "app": 1},
    "Entertainment": {"cinema": 3, "movie": 3, "theatre": 3, "theater": 3, "concert": 3, "club": 2,
                      "show": 1, "event": 1, "amusement": 3, "karaoke": 3},
    "Alcohol": {"wine": 3, "beer": 3, "liquor": 3, "whiskey": 3, "vodka": 3, "brewery": 3, "pub": 3,
                "cocktail": 3, "spirit": 2, "alcohol": 3, "bar": 1},
    "Gambling": {"casino": 4, "bet": 3, "betting": 4, "lottery": 3, "poker": 4, "slot": 3, "gambling": 4,
                 "sportsbook": 4},
}
PROHIBITED_BONUS = {"Alcohol": 2.0, "Gambling": 2.0}  # err on the side of surfacing risky spend for review


def _terms(text: str) -> set:
    doc = get_nlp()(text.lower())
    terms = set()
    for tok in doc:
        if not tok.is_alpha:
            continue
        terms.add(tok.lower_)
        if tok.lemma_:
            terms.add(tok.lemma_.lower())
        if tok.lower_.endswith("s") and len(tok.lower_) > 3:
            terms.add(tok.lower_[:-1])  # naive plural fallback when no lemmatizer is present
    return terms


def classify_expense(text: str, merchant: str = "") -> dict:
    """Return {category, confidence, scores, matched}. Unknown receipts fall back to 'Other'."""
    text = re.sub(r"\s+", " ", f"{merchant} {merchant} {text}")  # merchant counts double
    terms = _terms(text)
    scores, matched = {}, {}
    for cat, words in CATEGORY_KEYWORDS.items():
        hits = {w: wt for w, wt in words.items() if w in terms}
        if hits:
            scores[cat] = sum(hits.values()) * PROHIBITED_BONUS.get(cat, 1.0)
            matched[cat] = sorted(hits)
    if not scores:
        return {"category": "Other", "confidence": 0.3, "scores": {}, "matched": {}}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    total = sum(scores.values())
    confidence = round(min(0.99, 0.35 + 0.65 * (best_score / total) * min(1.0, best_score / 4)), 2)
    return {"category": best, "confidence": confidence, "scores": scores, "matched": matched.get(best, [])}
