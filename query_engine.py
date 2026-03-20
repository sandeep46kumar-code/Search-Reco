"""
query_engine.py
Layer 1 — Query Engine (the core).
Force segmentation · N-gram waste detection · Intent clustering · Cross-campaign dedup.
"""
from collections import defaultdict


# ── SEGMENTATION TERM LISTS ───────────────────────────────────────────────────
# Extend these lists in campaign_configs.business_context if needed per account.

BRAND_TERMS = {
    "paytm", "pay tm", "paytm gold", "paytm insurance", "paytm loans",
    "paytm money", "paytm recharge", "paytm mall", "paytm bank"
}

COMPETITOR_TERMS = {
    "phonepe", "phone pe", "gpay", "google pay", "groww", "zerodha",
    "hdfc gold", "icici gold", "sbi gold", "bajaj finserv", "policy bazaar",
    "policybazaar", "coverfox", "acko", "digit insurance"
}

INTENT_CLUSTERS = {
    "transactional":  ["buy", "purchase", "invest now", "open account", "download",
                       "apply", "get", "start", "book now", "order"],
    "comparative":    ["best", "vs", "versus", "compare", "top", "which", "review",
                       "better", "difference", "alternative"],
    "informational":  ["what is", "how to", "meaning", "explain", "guide", "learn",
                       "understand", "definition", "about", "why"],
    "price_seeking":  ["cheap", "low cost", "minimum", "fees", "charges", "free",
                       "discount", "offer", "cashback", "price", "rate", "return"],
    "navigational":   ["login", "app", "website", "official", "portal", "sign in",
                       "account", "dashboard", "support", "contact"],
}


def classify_query(query: str,
                   brand_terms: set = None,
                   competitor_terms: set = None) -> str:
    """Classify a query into brand / competitor / generic."""
    brands      = brand_terms or BRAND_TERMS
    competitors = competitor_terms or COMPETITOR_TERMS
    q = query.lower()
    if any(t in q for t in brands):
        return "brand"
    if any(t in q for t in competitors):
        return "competitor"
    return "generic"


def cluster_query(query: str) -> str:
    """Assign a query to an intent cluster."""
    q = query.lower()
    for cluster, signals in INTENT_CLUSTERS.items():
        if any(s in q for s in signals):
            return cluster
    return "unclassified"


# ── N-GRAM WASTE ANALYSIS ─────────────────────────────────────────────────────

def extract_ngrams(query: str, n: int) -> list[str]:
    words = query.lower().split()
    return [" ".join(words[i:i+n]) for i in range(len(words) - n + 1)]


def ngram_waste_report(search_terms: list[dict],
                       min_cost: float = 300,
                       min_clicks: int = 15) -> list[dict]:
    """
    Find n-gram patterns (1,2,3-word) with high spend and zero conversions.
    These are candidates for negative keywords.
    """
    stats = defaultdict(lambda: {"cost": 0, "conv": 0, "clicks": 0, "occurrences": 0})

    for row in search_terms:
        q = str(row.get("query", ""))
        for n in [1, 2, 3]:
            for gram in extract_ngrams(q, n):
                s = stats[gram]
                s["cost"]        += float(row.get("cost", 0))
                s["conv"]        += float(row.get("conversions", 0))
                s["clicks"]      += int(row.get("clicks", 0))
                s["occurrences"] += 1

    waste = []
    for gram, s in stats.items():
        if (s["cost"] >= min_cost and s["conv"] == 0 and s["clicks"] >= min_clicks):
            waste.append({
                "ngram":       gram,
                "cost":        round(s["cost"], 2),
                "clicks":      s["clicks"],
                "conversions": 0,
                "occurrences": s["occurrences"],
                "flag":        "WASTE",
                "action":      f"Add '{gram}' as negative keyword"
            })

    return sorted(waste, key=lambda x: -x["cost"])


# ── CROSS-CAMPAIGN DEDUPLICATION ──────────────────────────────────────────────

def detect_duplicates(search_terms: list[dict]) -> list[dict]:
    """
    Find queries appearing in multiple campaigns.
    These create internal auction competition and inflate CPCs.
    """
    query_campaigns = defaultdict(set)
    query_stats     = defaultdict(lambda: {"cost": 0, "conv": 0, "clicks": 0})

    for row in search_terms:
        q    = str(row.get("query", "")).lower()
        camp = str(row.get("campaign_name", ""))
        query_campaigns[q].add(camp)
        query_stats[q]["cost"]    += float(row.get("cost", 0))
        query_stats[q]["conv"]    += float(row.get("conversions", 0))
        query_stats[q]["clicks"]  += int(row.get("clicks", 0))

    dupes = []
    for q, camps in query_campaigns.items():
        if len(camps) > 1:
            s = query_stats[q]
            dupes.append({
                "query":       q,
                "campaigns":   list(camps),
                "flag":        "DUPLICATE",
                "cost":        round(s["cost"], 2),
                "conversions": s["conv"],
                "action":      f"Resolve overlap: add negative in lower-priority campaign"
            })

    return sorted(dupes, key=lambda x: -x["cost"])


# ── FULL QUERY SIGNAL TABLE ───────────────────────────────────────────────────

def build_query_signals(search_terms: list[dict],
                        brand_terms: set = None,
                        competitor_terms: set = None) -> list[dict]:
    """
    Enrich each search term row with: segment, intent_cluster, waste_flag, dupe_flag.
    This is the unified query signal table used by all downstream layers.
    """
    # Pre-compute duplicate set for O(1) lookup
    dupe_set = set()
    query_camps = defaultdict(set)
    for row in search_terms:
        q    = str(row.get("query", "")).lower()
        camp = str(row.get("campaign_name", ""))
        query_camps[q].add(camp)
    for q, camps in query_camps.items():
        if len(camps) > 1:
            dupe_set.add(q)

    enriched = []
    for row in search_terms:
        q       = str(row.get("query", ""))
        cost    = float(row.get("cost", 0))
        clicks  = int(row.get("clicks", 0))
        conv    = float(row.get("conversions", 0))
        impr    = int(row.get("impressions", 0))
        ctr     = clicks / impr if impr > 0 else 0
        cvr     = conv / clicks if clicks > 0 else 0

        enriched.append({
            **row,
            "query":          q,
            "segment":        classify_query(q, brand_terms, competitor_terms),
            "intent_cluster": cluster_query(q),
            "ctr":            round(ctr * 100, 3),
            "cvr":            round(cvr * 100, 3),
            "cpa":            round(cost / conv, 2) if conv > 0 else None,
            "dupe_flag":      q.lower() in dupe_set,
        })

    return enriched


def summarise_query_signals(query_signals: list[dict]) -> dict:
    """
    High-level summary of the query pool for injection into Claude prompts.
    """
    total_cost    = sum(r.get("cost", 0) for r in query_signals)
    total_conv    = sum(r.get("conversions", 0) for r in query_signals)
    zero_conv     = [r for r in query_signals if r.get("conversions", 0) == 0]
    zero_conv_cost= sum(r.get("cost", 0) for r in zero_conv)

    segment_counts = defaultdict(int)
    intent_counts  = defaultdict(int)
    for r in query_signals:
        segment_counts[r.get("segment", "unknown")] += 1
        intent_counts[r.get("intent_cluster", "unclassified")] += 1

    waste   = ngram_waste_report(query_signals)
    dupes   = detect_duplicates(query_signals)

    return {
        "total_queries":      len(query_signals),
        "total_cost":         round(total_cost, 2),
        "total_conversions":  total_conv,
        "zero_conv_queries":  len(zero_conv),
        "zero_conv_cost":     round(zero_conv_cost, 2),
        "zero_conv_pct":      round(zero_conv_cost / total_cost * 100, 1) if total_cost > 0 else 0,
        "segment_breakdown":  dict(segment_counts),
        "intent_breakdown":   dict(intent_counts),
        "waste_patterns":     waste[:10],
        "duplicate_queries":  dupes[:10],
    }


if __name__ == "__main__":
    from data_pull import pull_search_term_data
    import os
    df = pull_search_term_data(os.getenv("CUSTOMER_ID", "demo"))
    rows = df.to_dict("records")
    signals = build_query_signals(rows)
    summary = summarise_query_signals(signals)
    print(f"Total queries: {summary['total_queries']}")
    print(f"Zero-conv cost: Rs {summary['zero_conv_cost']:,.0f} ({summary['zero_conv_pct']}%)")
    print(f"Waste patterns: {len(summary['waste_patterns'])}")
    print(f"Duplicate queries: {len(summary['duplicate_queries'])}")
