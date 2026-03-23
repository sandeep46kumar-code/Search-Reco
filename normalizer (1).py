"""
normalizer.py
Converts raw API DataFrames into the canonical signal schema.
KPI-aware: handles CPA, ROAS, CPL, CPC_CAP, VOLUME.
"""
import math
from memory import get_all_active_configs, DEFAULT_LEVER_WEIGHTS
from stability import analyze_stability, blend_primary_recency, get_attribution_windows


def compute_primary_metric(cost: float, conversions: float, conv_value: float,
                            clicks: float, kpi: str) -> float:
    """Compute the primary optimisation metric based on KPI type."""
    if kpi == "CPA":
        return cost / conversions if conversions > 0 else math.inf
    elif kpi == "ROAS":
        return conv_value / cost if cost > 0 else 0.0
    elif kpi == "CPL":
        return cost / conversions if conversions > 0 else math.inf
    elif kpi == "CPC_CAP":
        return cost / clicks if clicks > 0 else math.inf
    elif kpi == "VOLUME":
        return conversions
    return math.inf


def classify_campaign(metric: float, target: float, kpi: str) -> str:
    """Classify as SCALABLE / SATURATION / DIMINISHING based on KPI type."""
    if metric == math.inf or metric == 0:
        return "LOW_CONFIDENCE"
    # For ROAS: higher is better. For everything else: lower is better.
    if kpi == "ROAS":
        ratio = target / metric if metric > 0 else 0
    elif kpi == "VOLUME":
        return "SCALABLE"  # Volume campaigns: classify by IS, not metric ratio
    else:
        ratio = metric / target

    if ratio <= 1.0:
        return "SCALABLE"
    elif ratio <= 1.3:
        return "SATURATION"
    else:
        return "DIMINISHING"


def assign_confidence(conversions: float, clicks: float) -> str:
    if conversions >= 30 and clicks >= 300:
        return "SUFFICIENT"
    elif conversions >= 15 or clicks >= 150:
        return "WEAK_SIGNAL"
    return "LOW_CONFIDENCE"


def normalize(df, configs: list[dict] = None, attribution_n: int = 14,
               recency_df=None) -> list[dict]:
    """
    Convert raw campaign DataFrame to canonical signal list.
    configs: list of campaign config dicts from DB (optional — uses defaults if None).
    """
    # Build config lookup by campaign name
    config_map = {}
    if configs:
        for c in configs:
            config_map[c["campaign_id"]] = c

    # Build recency metric lookup by campaign name (for blend_primary_recency)
    recency_map = {}
    if recency_df is not None and not recency_df.empty:
        for _, rrow in recency_df.iterrows():
            camp = str(rrow.get("campaign_name", rrow.get("campaign_id", "")))
            rcost = float(rrow.get("cost", 0) or 0)
            rconv = float(rrow.get("conversions", 0) or 0)
            rcv   = float(rrow.get("conversion_value", 0) or 0)
            rclk  = float(rrow.get("clicks", 0) or 0)
            rcfg  = config_map.get(camp, {})
            rkpi  = rcfg.get("kpi", "CPA")
            rtgt  = float(rcfg.get("target_value", 250))
            rm    = compute_primary_metric(rcost, rconv, rcv, rclk, rkpi)
            if math.isfinite(rm):
                recency_map[camp] = rm

    signals = []
    for _, row in df.iterrows():
        campaign_id   = str(row.get("campaign_id", row.get("campaign_name", "")))
        campaign_name = str(row.get("campaign_name", campaign_id))

        # Look up config — fall back to defaults
        cfg = config_map.get(campaign_id, config_map.get(campaign_name, {}))
        kpi          = cfg.get("kpi", "CPA")
        target       = float(cfg.get("target_value", 250))
        product      = cfg.get("product", "unknown")
        max_actions  = cfg.get("max_actions_per_day", 2)
        lever_weights = cfg.get("lever_weights", DEFAULT_LEVER_WEIGHTS)
        biz_ctx      = cfg.get("business_context", {})

        # Raw metrics
        cost_7d   = float(row.get("cost_7d", 0))
        clicks_7d = float(row.get("clicks_7d", 0))
        impr_7d   = float(row.get("impressions_7d", 0))
        conv_7d   = float(row.get("conversions_7d", 0))
        cv_7d     = float(row.get("conversion_value_7d", 0))
        cost_14d  = float(row.get("cost_14d", 0))
        conv_14d  = float(row.get("conversions_14d", 0))
        cv_14d    = float(row.get("conversion_value_14d", 0))

        # IS fields
        is_lost_budget = float(row.get("is_lost_budget_7d", 0))
        is_lost_rank   = float(row.get("is_lost_rank_7d", 0))
        is_abs_top     = float(row.get("is_abs_top_7d", 0))
        is_captured    = float(row.get("is_captured_7d", 0))

        # Computed metrics
        metric_7d  = compute_primary_metric(cost_7d, conv_7d, cv_7d, clicks_7d, kpi)
        metric_14d = compute_primary_metric(cost_14d, conv_14d, cv_14d, clicks_7d, kpi)
        ctr        = clicks_7d / impr_7d if impr_7d > 0 else 0
        cvr        = conv_7d / clicks_7d if clicks_7d > 0 else 0

        # Volatility: % swing between 7d and 14d metric
        if metric_14d > 0 and math.isfinite(metric_14d) and math.isfinite(metric_7d):
            volatility = abs(metric_7d - metric_14d) / metric_14d
        else:
            volatility = 0.0

        confidence     = assign_confidence(conv_7d, clicks_7d)
        classification = "LOW_CONFIDENCE"
        if confidence == "SUFFICIENT":
            classification = classify_campaign(metric_7d, target, kpi)

        signals.append({
            "campaign_id":       campaign_id,
            "name":              campaign_name,
            "product":           product,
            "kpi":               kpi,
            "target_value":      target,
            "classification":    classification,
            "confidence":        confidence,
            "unstable":          volatility > 0.4,

            # Raw window data
            "cost_7d":           round(cost_7d, 2),
            "clicks_7d":         int(clicks_7d),
            "impressions_7d":    int(impr_7d),
            "conversions_7d":    int(conv_7d),
            "conversion_value_7d": round(cv_7d, 2),
            "cost_14d":          round(cost_14d, 2),
            "conversions_14d":   int(conv_14d),

            # Computed
            "primary_metric_7d": round(metric_7d, 2) if math.isfinite(metric_7d) else None,
            "primary_metric_14d":round(metric_14d, 2) if math.isfinite(metric_14d) else None,
            "ctr":               round(ctr * 100, 3),
            "cvr":               round(cvr * 100, 3),
            "cpa_volatility":    round(volatility, 3),
            "attribution_n":     attribution_n,
            "data_window":        get_attribution_windows(attribution_n)["primary_label"],
            "recency_label":      get_attribution_windows(attribution_n)["recency_label"],
            "full_window_label":  get_attribution_windows(attribution_n)["full_label"],
            "stability_flag":     "UNKNOWN",
            "weighted_metric":    None,
            "recency_conflict":   False,
            "recency_flag":       "NO_RECENCY_DATA",
            "recency_cpa":        None,
            "recency_delta_pct":  None,

            # IS fields
            "is_lost_budget":    round(is_lost_budget, 3),
            "is_lost_rank":      round(is_lost_rank, 3),
            "is_abs_top":        round(is_abs_top, 3),
            "is_captured":       round(is_captured, 3),

            # Config passthrough
            "max_actions_per_day": max_actions,
            "lever_weights":     lever_weights,
            "business_context":  biz_ctx,
        })

    # Now compute the real blend for each signal using recency data
    for s in signals:
        camp = s["name"]
        primary_m = s["primary_metric_7d"] or s.get("primary_metric_14d")
        recency_m = recency_map.get(camp)

        if primary_m and primary_m > 0:
            blend = blend_primary_recency(primary_m, recency_m)
            s["weighted_metric"]   = blend["blended_metric"]
            s["recency_flag"]      = blend["trend_signal"]
            s["recency_cpa"]       = blend.get("recency_metric")
            s["recency_delta_pct"] = blend.get("recency_delta")
            s["recency_used"]      = blend.get("recency_used", False)

            # CONFLICT: recency CPA >20% worse than primary — flag it
            delta = blend.get("recency_delta", 0)
            kpi = s.get("kpi", "CPA")
            if kpi in ("CPA","CPL","CPC_CAP"):
                conflict = delta is not None and delta > 20
                improving = delta is not None and delta < -15
            else:  # ROAS/VOLUME — higher is better
                conflict  = delta is not None and delta < -20
                improving = delta is not None and delta > 15

            s["recency_conflict"] = conflict
            if conflict:
                s["stability_flag"] = "CONFLICT"
                s["recency_note"] = (
                    f"Recency window ({s.get('recency_label','')}) shows "
                    f"{abs(delta):.0f}% worse {kpi} than primary window. "
                    f"Primary CPA ₹{primary_m:,.0f} used for recommendation. "
                    f"Investigate before acting."
                )
            elif improving:
                s["recency_note"] = (
                    f"Recency signal shows {abs(delta):.0f}% improvement — "
                    f"performance may be better than primary window suggests."
                )
            else:
                s["recency_note"] = blend["trend_signal"].replace("_", " ").title()

    return signals


if __name__ == "__main__":
    from data_pull import pull_campaign_data
    import os
    df = pull_campaign_data(os.getenv("CUSTOMER_ID", "demo"))
    signals = normalize(df)
    for s in signals:
        print(f"{s['name'][:40]:40} | {s['classification']:15} | "
              f"metric={s['primary_metric_7d']} | conf={s['confidence']}")
