"""
intelligence.py
Layer 3 — KPI-aware marginal analysis engine.
Computes spend-tier CPA curves and elasticity signals per campaign.
"""
import math


def build_spend_tiers(signals: list[dict]) -> list[dict]:
    """
    Sort campaigns by cost, split into 4 tiers, compute marginal metric per tier.
    Marginal CPA = delta_cost / delta_conversions between adjacent tiers.
    """
    sufficient = sorted(
        [s for s in signals if s["confidence"] == "SUFFICIENT"],
        key=lambda x: x["cost_7d"]
    )
    if len(sufficient) < 2:
        return []

    n = len(sufficient)
    tier_size = max(1, n // 4)
    tiers = []

    for i in range(0, n, tier_size):
        tier = sufficient[i:i + tier_size]
        total_cost = sum(s["cost_7d"] for s in tier)
        total_conv = sum(s["conversions_7d"] for s in tier)
        avg_metric = (
            total_cost / total_conv if total_conv > 0 and tier[0]["kpi"] == "CPA"
            else total_conv / total_cost if total_cost > 0
            else 0
        )
        tiers.append({
            "tier": i // tier_size + 1,
            "campaigns": [s["name"] for s in tier],
            "total_cost": round(total_cost, 2),
            "total_conversions": total_conv,
            "avg_metric": round(avg_metric, 2),
        })

    # Compute marginal metric between adjacent tiers
    for i in range(1, len(tiers)):
        delta_cost = tiers[i]["total_cost"] - tiers[i-1]["total_cost"]
        delta_conv = tiers[i]["total_conversions"] - tiers[i-1]["total_conversions"]
        tiers[i]["marginal_metric"] = (
            round(delta_cost / delta_conv, 2) if delta_conv > 0 else None
        )

    return tiers


def elasticity_signal(signal: dict) -> str:
    """
    Classify the scaling opportunity based on IS loss pattern.
    Returns: SCALE_BUDGET | SCALE_BIDS | SATURATED | DIMINISHING | HOLD
    """
    lost_budget = signal.get("is_lost_budget", 0)
    lost_rank   = signal.get("is_lost_rank", 0)
    klass       = signal.get("classification", "")

    if klass == "SCALABLE":
        if lost_budget > 0.2:
            return "SCALE_BUDGET"
        if lost_rank > 0.2:
            return "SCALE_BIDS"
        return "HOLD"
    elif klass == "SATURATION":
        return "HOLD"
    elif klass == "DIMINISHING":
        return "DIMINISHING"
    return "HOLD"


def run_intelligence(signals: list[dict]) -> dict:
    """Full intelligence pass — returns spend tiers + per-campaign elasticity."""
    tiers = build_spend_tiers(signals)
    per_campaign = []
    for s in signals:
        per_campaign.append({
            "name":              s["name"],
            "classification":    s["classification"],
            "confidence":        s["confidence"],
            "elasticity":        elasticity_signal(s),
            "primary_metric":    s.get("primary_metric_7d"),
            "target":            s.get("target_value"),
            "is_lost_budget":    s.get("is_lost_budget"),
            "is_lost_rank":      s.get("is_lost_rank"),
        })
    return {"spend_tiers": tiers, "campaign_elasticity": per_campaign}
