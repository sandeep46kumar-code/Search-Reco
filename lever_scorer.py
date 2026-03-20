"""
lever_scorer.py
Layer 6 — Scores all 10 levers per campaign before generation.
Only levers above threshold enter the recommendation engine.
"""

LEVER_THRESHOLD = 3.0

ALL_LEVERS = [
    "budget_reallocation", "bid_adjustment", "ad_copy",
    "search_term_mining", "match_type", "landing_page",
    "audience_layering", "dayparting", "device_reallocation", "quality_score"
]


def _data_signal(lever: str, signal: dict, query_summary: dict = None) -> float:
    """Compute a 0.0–1.0 data signal for each lever from campaign signals."""
    qs = query_summary or {}

    if lever == "budget_reallocation":
        # High budget-lost IS on scalable campaign = strong signal
        if signal.get("classification") == "SCALABLE":
            return min(signal.get("is_lost_budget", 0) * 2, 1.0)
        return 0.0

    elif lever == "bid_adjustment":
        # High rank-lost IS on scalable campaign = strong signal
        if signal.get("classification") == "SCALABLE":
            return min(signal.get("is_lost_rank", 0) * 2, 1.0)
        return 0.0

    elif lever == "ad_copy":
        # CVR below average or intent mismatch = strong signal
        cvr = signal.get("cvr", 0)
        return min(max(0.0, (5.0 - cvr) / 5.0), 1.0)  # 0% CVR = 1.0, 5% CVR = 0.0

    elif lever == "search_term_mining":
        # % of spend on zero-conversion queries
        zero_pct = qs.get("zero_conv_pct", 0)
        return min(zero_pct / 100, 1.0)

    elif lever == "match_type":
        # Low CVR with generic segment = match type bleed likely
        cvr  = signal.get("cvr", 0)
        return 0.8 if cvr < 3.0 else 0.3

    elif lever == "landing_page":
        # CVR drop with stable query mix
        metric_7d  = signal.get("primary_metric_7d") or 0
        metric_14d = signal.get("primary_metric_14d") or metric_7d
        kpi = signal.get("kpi", "CPA")
        if kpi in ("CPA", "CPL") and metric_14d > 0:
            degradation = (metric_7d - metric_14d) / metric_14d
            return min(max(degradation, 0), 1.0)
        return 0.2

    elif lever == "audience_layering":
        # Baseline — needs device/audience breakdown to be precise
        return 0.3

    elif lever == "dayparting":
        # Baseline — needs hourly data to be precise
        return 0.3

    elif lever == "device_reallocation":
        # Baseline — needs device-level data
        return 0.35

    elif lever == "quality_score":
        # Diminishing campaigns often have QS issues
        return 0.6 if signal.get("classification") == "DIMINISHING" else 0.2

    return 0.0


def score_levers(signal: dict,
                 accuracy_scores: dict = None,
                 query_summary: dict = None,
                 threshold: float = LEVER_THRESHOLD) -> dict:
    """
    Score all levers for a campaign. Returns only levers above threshold,
    sorted by score descending.

    Final score = weight × data_signal × historical_accuracy
    """
    weights   = signal.get("lever_weights", {})
    accuracy  = accuracy_scores or {}
    scores    = {}

    for lever in ALL_LEVERS:
        weight      = float(weights.get(lever, 5))
        data_sig    = _data_signal(lever, signal, query_summary)
        hist_acc    = float(accuracy.get(lever, 0.5))  # default 50% accuracy
        score       = round(weight * data_sig * hist_acc, 3)
        scores[lever] = score

    # Filter and sort
    active = {
        k: v for k, v in sorted(scores.items(), key=lambda x: -x[1])
        if v >= threshold
    }
    return active


if __name__ == "__main__":
    sample_signal = {
        "name": "Search_Digital_Gold_Generic",
        "classification": "SCALABLE",
        "confidence": "SUFFICIENT",
        "cvr": 2.1,
        "is_lost_budget": 0.34,
        "is_lost_rank": 0.18,
        "kpi": "CPA",
        "primary_metric_7d": 210,
        "primary_metric_14d": 195,
        "lever_weights": {
            "budget_reallocation": 8, "bid_adjustment": 6, "ad_copy": 9,
            "search_term_mining": 7, "match_type": 5, "landing_page": 4,
            "audience_layering": 3, "dayparting": 4, "device_reallocation": 5,
            "quality_score": 6
        }
    }
    scores = score_levers(sample_signal)
    for lever, score in scores.items():
        print(f"  {lever:25} {score:.3f}")
