"""
copy_intelligence.py
Layer 4 — Position-adjusted CTR analysis and intent × copy matrix.
Strips rank effect before attributing performance to ad copy.
"""
from collections import defaultdict

# Position CTR index — calibrate this to your account over time
# These are industry averages. After 30 days of data, replace with your account's actual values.
POSITION_CTR_INDEX = {1: 1.0, 2: 0.75, 3: 0.55, 4: 0.40}


def position_adjusted_ctr(raw_ctr: float, avg_position: float) -> float:
    """Normalise CTR to position-1 equivalent. Never compare raw CTR between ads."""
    pos   = round(min(max(avg_position, 1.0), 4.0))
    index = POSITION_CTR_INDEX.get(pos, 0.35)
    return raw_ctr / index if index > 0 else raw_ctr


def build_intent_copy_matrix(ad_search_term_data: list[dict]) -> dict:
    """
    For each ad, compute CTR and CVR broken out by intent cluster.
    Returns: { ad_id: { intent_cluster: { ctr, cvr, volume } } }
    """
    from query_engine import cluster_query
    matrix = defaultdict(lambda: defaultdict(lambda: {"clicks": 0, "conv": 0, "impr": 0}))

    for row in ad_search_term_data:
        intent = cluster_query(str(row.get("query", "")))
        ad_id  = str(row.get("ad_id", row.get("ad_group", "unknown")))
        m      = matrix[ad_id][intent]
        m["clicks"] += int(row.get("clicks", 0))
        m["conv"]   += float(row.get("conversions", 0))
        m["impr"]   += int(row.get("impressions", 0))

    result = {}
    for ad_id, intents in matrix.items():
        result[ad_id] = {}
        for intent, m in intents.items():
            result[ad_id][intent] = {
                "ctr":    round(m["clicks"] / m["impr"] * 100, 3) if m["impr"] > 0 else 0,
                "cvr":    round(m["conv"] / m["clicks"] * 100, 3) if m["clicks"] > 0 else 0,
                "volume": m["clicks"],
            }
    return result


def detect_brand_over_index(recommendations: list[dict],
                             threshold: float = 0.6) -> bool:
    """
    Returns True if more than `threshold` fraction of recs are brand-campaign-related.
    """
    if not recommendations:
        return False
    brand_recs = sum(1 for r in recommendations if r.get("segment") == "brand")
    return brand_recs / len(recommendations) > threshold


def check_incrementality(signal: dict) -> str:
    """
    Flag whether a scaling recommendation is incremental growth or demand harvesting.
    HARVESTING: brand IS already > 85% — scaling only captures existing demand.
    GROWTH: meaningful IS gap remains.
    """
    is_captured = signal.get("is_captured", 0)
    segment     = signal.get("segment", "generic")

    if segment == "brand" and is_captured > 0.85:
        return "HARVESTING"
    if signal.get("is_lost_budget", 0) > 0.15 or signal.get("is_lost_rank", 0) > 0.15:
        return "GROWTH"
    return "NEUTRAL"
