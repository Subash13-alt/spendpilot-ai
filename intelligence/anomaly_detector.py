"""Statistical and Isolation Forest anomaly scoring against historical spending.

Returns scores that mean "unusual compared with history", never "fraud".
"""
import math
from typing import List, Optional

import numpy as np

MIN_ISO_SAMPLES = 25
MIN_STAT_SAMPLES = 4
CATEGORIES = ["Accommodation", "Food", "Transportation", "Office Supplies", "Software",
              "Entertainment", "Alcohol", "Gambling", "Other"]


def zscore_check(amount: float, history: List[float]) -> dict:
    """Robust z-score using median/MAD so one past outlier does not mask the next."""
    if len(history) < MIN_STAT_SAMPLES:
        return {"available": False, "z": None, "score": 0.0, "n": len(history)}
    arr = np.asarray(history, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    scale = 1.4826 * mad if mad > 0 else max(float(arr.std()), med * 0.1, 1.0)
    z = (amount - med) / scale
    score = max(0.0, min(1.0, (z - 2.0) / 4.0))  # z<=2 -> 0, z>=6 -> 1
    return {"available": True, "z": round(z, 2), "median": round(med, 2), "score": round(score, 3), "n": len(history)}


def _features(rows: List[dict]) -> np.ndarray:
    feats = []
    for r in rows:
        cat_idx = CATEGORIES.index(r["category"]) if r["category"] in CATEGORIES else len(CATEGORIES) - 1
        feats.append([math.log1p(float(r["amount"])), cat_idx, int(r.get("weekday", 0))])
    return np.asarray(feats, dtype=float)


def isolation_forest_score(candidate: dict, history_rows: List[dict], seed: int = 42) -> dict:
    """Fit on organisation history (excluding the candidate) and score the candidate in [0, 1]."""
    if len(history_rows) < MIN_ISO_SAMPLES:
        return {"available": False, "score": 0.0, "n": len(history_rows)}
    from sklearn.ensemble import IsolationForest
    model = IsolationForest(n_estimators=150, contamination="auto", random_state=seed)
    model.fit(_features(history_rows))
    raw = float(-model.score_samples(_features([candidate]))[0])  # ~0.35 normal ... 0.7+ anomalous
    score = max(0.0, min(1.0, (raw - 0.5) / 0.2))
    return {"available": True, "score": round(score, 3), "raw": round(raw, 3), "n": len(history_rows)}
