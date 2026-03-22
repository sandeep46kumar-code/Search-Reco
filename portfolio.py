"""
portfolio.py
Layer 5 — Portfolio-level checks.
Cannibalization detection · Category budget caps · Account-level impact scoring.
"""
from collections import defaultdict
from memory import get_conn
import json


def get_category_caps() -> dict:
    """Load category budget caps from DB."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT category, daily_budget_cap FROM portfolio_config")
                return {r["category"]: r["daily_budget_cap"] for r in cur.fetchall()}
    except Exception:
        return {}


def query_overlap_score(camp_a: str, camp_b: str,
                        all_query_signals: list[dict]) -> float:
    """
    Compute fraction of Camp A's queries that also appear in Camp B.
    Returns 0.0–1.0. Above 0.2 = cannibalization risk.
    """
    queries_a = {r["query"].lower() for r in all_query_signals
                 if r.get("campaign_name") == camp_a}
    queries_b = {r["query"].lower() for r in all_query_signals
                 if r.get("campaign_name") == camp_b}
    if not queries_a:
        return 0.0
    overlap = len(queries_a & queries_b)
    return round(overlap / len(queries_a), 3)


def compute_portfolio_impact(rec: dict, all_signals: list[dict],
                              all_query_signals: list[dict] = None) -> dict:
    """
    Evaluate a recommendation's portfolio-level impact.
    Returns portfolio_verdict: CLEAR | BLOCKED + reasons.
    """
    category_caps  = get_category_caps()
    campaign_name  = rec.get("campaign", "")

    # Find campaign signal
    signal = next((s for s in all_signals if s["name"] == campaign_name), {})
    product = signal.get("product", "unknown")

    # Category budget headroom
    category_spend = sum(
        s.get("cost_7d", 0) for s in all_signals
        if s.get("product") == product
    )
    cap = category_caps.get(product, 0)
    headroom = (cap - category_spend / 7) if cap > 0 else float("inf")

    # Cannibalization check for reallocation recs
    cannibal_risk = 0.0
    if rec.get("type") == "budget_reallocation" and all_query_signals:
        to_camp = rec.get("to_campaign", "")
        if to_camp:
            cannibal_risk = query_overlap_score(campaign_name, to_camp, all_query_signals)

    # Account-level conversion impact
    est_delta    = rec.get("est_conv_delta", 0)
    account_conv = sum(s.get("conversions_7d", 0) for s in all_signals)
    impact_pct   = round(est_delta / account_conv * 100, 1) if account_conv > 0 else 0

    blocked_reasons = []
    if cap > 0 and headroom <= 0:
        blocked_reasons.append(f"Category '{product}' daily budget cap exceeded")
    if cannibal_risk > 0.2:
        blocked_reasons.append(f"Cannibalization risk {cannibal_risk:.0%} with target campaign")

    return {
        "category":              product,
        "category_headroom":     None if headroom == float("inf") else round(headroom, 2),
        "cannibalization_risk":  cannibal_risk,
        "account_impact_pct":    impact_pct,
        "portfolio_verdict":     "BLOCKED" if blocked_reasons else "CLEAR",
        "blocked_reasons":       blocked_reasons,
    }
