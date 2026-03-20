"""
cause_diagnosis.py
Layer 2 — Cause Diagnosis.
Deterministic decision trees — no Claude call.
Every metric change must be explained before any recommendation is formed.
"""
import math


def diagnose_ctr_drop(signal: dict, prev_signal: dict = None) -> dict:
    """
    Diagnose why CTR dropped. Returns a cause object.
    If no previous signal is available, uses internal consistency checks.
    """
    ctr_7d   = signal.get("ctr", 0)
    is_rank  = signal.get("is_lost_rank", 0)
    is_abs   = signal.get("is_abs_top", 0)
    is_cap   = signal.get("is_captured", 0)

    ctr_14d  = signal.get("ctr_14d", ctr_7d)
    ctr_delta = ctr_7d - ctr_14d

    # No drop — nothing to diagnose
    if ctr_delta >= 0 or abs(ctr_delta) < 0.3:
        return {
            "metric": "CTR", "delta": round(ctr_delta, 4),
            "diagnosed_cause": "NO_CHANGE",
            "evidence": "CTR is stable or improving.",
            "confidence": "HIGH",
            "action_direction": None
        }

    # Rank loss — position degraded
    if is_rank > 0.25 or is_abs < 0.3:
        cause = "RANK_LOSS"
        evidence = (f"Lost IS (rank) = {is_rank:.0%}, abs-top IS = {is_abs:.0%}. "
                    f"Position degraded — fix bids, not copy.")
        direction = "bid_adjustment"

    # Budget constraint eating impression share
    elif signal.get("is_lost_budget", 0) > 0.3:
        cause = "BUDGET_CONSTRAINT"
        evidence = (f"Lost IS (budget) = {signal.get('is_lost_budget', 0):.0%}. "
                    f"Impressions being rationed — some hours not showing.")
        direction = "budget_reallocation"

    # Segment mix shift — brand/generic ratio changed
    elif signal.get("classification") == "LOW_CONFIDENCE":
        cause = "INSUFFICIENT_SIGNAL"
        evidence = "Not enough data to diagnose CTR change reliably."
        direction = None

    # Position stable, CTR fell — creative issue
    else:
        cause = "CREATIVE_DEGRADATION"
        evidence = (f"Position appears stable (IS rank {is_rank:.0%}) but CTR fell "
                    f"{abs(ctr_delta):.2f}pp. Ad fatigue or relevance degradation likely.")
        direction = "ad_copy"

    return {
        "metric": "CTR",
        "delta": round(ctr_delta, 4),
        "diagnosed_cause": cause,
        "evidence": evidence,
        "confidence": "HIGH" if abs(ctr_delta) > 0.5 else "MEDIUM",
        "action_direction": direction
    }


def diagnose_cvr_drop(signal: dict, query_signals: list[dict] = None) -> dict:
    """
    Diagnose why CVR dropped. Checks intent mix shift, device issues, landing page.
    """
    cvr_7d    = signal.get("cvr", 0)
    metric_7d = signal.get("primary_metric_7d")
    metric_14d= signal.get("primary_metric_14d")

    if metric_7d is None or metric_14d is None:
        return {
            "metric": "CVR", "delta": 0,
            "diagnosed_cause": "INSUFFICIENT_SIGNAL",
            "evidence": "Missing 14d baseline — cannot compute CVR delta.",
            "confidence": "LOW", "action_direction": None
        }

    # For CPA: higher 7d vs 14d means CVR dropped (more cost per conv)
    # For ROAS: lower means CVR dropped
    kpi = signal.get("kpi", "CPA")
    if kpi in ("CPA", "CPL", "CPC_CAP"):
        delta = metric_14d - metric_7d  # negative = got worse (CPA rose)
        dropped = metric_7d > metric_14d * 1.05  # 5% threshold
    else:  # ROAS, VOLUME
        delta = metric_7d - metric_14d  # negative = got worse
        dropped = metric_7d < metric_14d * 0.95

    if not dropped:
        return {
            "metric": "CVR", "delta": round(delta, 4),
            "diagnosed_cause": "NO_CHANGE",
            "evidence": "CVR/efficiency is stable — no action needed.",
            "confidence": "HIGH", "action_direction": None
        }

    # Check intent mix shift using query signals
    intent_shift = 0.0
    if query_signals:
        campaign_queries = [
            q for q in query_signals
            if q.get("campaign_name") == signal.get("name")
        ]
        if campaign_queries:
            transactional = sum(
                1 for q in campaign_queries
                if q.get("intent_cluster") == "transactional"
            )
            intent_shift = transactional / len(campaign_queries)

    if intent_shift < 0.2:
        cause = "INTENT_MISMATCH"
        evidence = (f"Transactional query share is low ({intent_shift:.0%}). "
                    f"Broad/phrase match is pulling in informational traffic. "
                    f"Tighten match types or add negative keywords.")
        direction = "match_type"
    elif signal.get("classification") == "DIMINISHING":
        cause = "SATURATION_CVR"
        evidence = ("Campaign is in diminishing returns. Additional volume "
                    "likely coming from lower-intent queries.")
        direction = "budget_reallocation"
    else:
        cause = "LANDING_PAGE_OR_OFFER"
        evidence = ("CVR dropped with stable query intent mix. "
                    "Check for landing page changes, offer changes, or UX issues.")
        direction = "landing_page"

    return {
        "metric": "CVR",
        "delta": round(delta, 4),
        "diagnosed_cause": cause,
        "evidence": evidence,
        "confidence": "MEDIUM",
        "action_direction": direction
    }


def diagnose_cpc_increase(signal: dict, account_signals: list[dict] = None) -> dict:
    """
    Diagnose why CPC increased. Checks market pressure, QS, isolated changes.
    """
    cost_7d   = signal.get("cost_7d", 0)
    clicks_7d = signal.get("clicks_7d", 1)
    cost_14d  = signal.get("cost_14d", 0)
    conv_14d  = signal.get("conversions_14d", 1)

    cpc_7d  = cost_7d / clicks_7d if clicks_7d > 0 else 0
    cpc_14d = (cost_14d / (signal.get("clicks_14d") or clicks_7d * 2)
               if cost_14d > 0 else cpc_7d)

    delta = cpc_7d - cpc_14d

    if delta <= 0 or delta < 2:
        return {
            "metric": "CPC", "delta": round(delta, 2),
            "diagnosed_cause": "NO_CHANGE",
            "evidence": "CPC is stable — no action needed.",
            "confidence": "HIGH", "action_direction": None
        }

    # Check if CPC rising account-wide (market pressure)
    account_rising = False
    if account_signals:
        rising_count = sum(
            1 for s in account_signals
            if s.get("cost_7d", 0) / max(s.get("clicks_7d", 1), 1) >
               s.get("cost_14d", 0) / max(s.get("clicks_7d", 1) * 2, 1)
        )
        account_rising = rising_count > len(account_signals) * 0.5

    if account_rising:
        cause = "MARKET_AUCTION_PRESSURE"
        evidence = (f"CPC rising across multiple campaigns (+Rs {delta:.0f} on this one). "
                    f"Likely competitive pressure or seasonal demand spike.")
        direction = "bid_adjustment"
    elif signal.get("is_lost_rank", 0) < 0.1 and signal.get("is_abs_top", 0) > 0.7:
        cause = "OVERBIDDING"
        evidence = (f"High abs-top IS ({signal.get('is_abs_top',0):.0%}) with rising CPC. "
                    f"Already at top position — further bidding wastes budget.")
        direction = "bid_adjustment"
    else:
        cause = "ISOLATED_PRESSURE"
        evidence = (f"CPC increased Rs {delta:.0f} isolated to this campaign. "
                    f"Check for new competitor entries or QS changes.")
        direction = "quality_score"

    return {
        "metric": "CPC",
        "delta": round(delta, 2),
        "diagnosed_cause": cause,
        "evidence": evidence,
        "confidence": "MEDIUM",
        "action_direction": direction
    }


def run_all_diagnoses(signal: dict,
                      query_signals: list[dict] = None,
                      account_signals: list[dict] = None) -> dict:
    """
    Run all three diagnosis trees for a single campaign signal.
    Returns a dict with CTR, CVR, and CPC diagnoses.
    """
    ctr_diag = diagnose_ctr_drop(signal)
    cvr_diag = diagnose_cvr_drop(signal, query_signals)
    cpc_diag = diagnose_cpc_increase(signal, account_signals)

    # Determine if DO_NOTHING should be triggered
    all_causes = [
        ctr_diag["diagnosed_cause"],
        cvr_diag["diagnosed_cause"],
        cpc_diag["diagnosed_cause"],
    ]

    do_nothing_triggers = []
    if signal.get("unstable"):
        do_nothing_triggers.append("CPA volatility > 40% — data is unstable")
    if signal.get("confidence") == "LOW_CONFIDENCE":
        do_nothing_triggers.append("Insufficient data — < 30 conversions or < 300 clicks")
    if all(c in ("NO_CHANGE", "INSUFFICIENT_SIGNAL") for c in all_causes):
        do_nothing_triggers.append("All metrics stable — no diagnosed changes")

    return {
        "campaign":            signal.get("name"),
        "ctr_diagnosis":       ctr_diag,
        "cvr_diagnosis":       cvr_diag,
        "cpc_diagnosis":       cpc_diag,
        "do_nothing_triggers": do_nothing_triggers,
        "should_do_nothing":   len(do_nothing_triggers) > 0,
    }


if __name__ == "__main__":
    from data_pull import pull_campaign_data
    from normalizer import normalize
    import os
    df = pull_campaign_data(os.getenv("CUSTOMER_ID", "demo"))
    signals = normalize(df)
    for s in signals[:3]:
        result = run_all_diagnoses(s)
        print(f"\n{result['campaign']}")
        print(f"  CTR: {result['ctr_diagnosis']['diagnosed_cause']}")
        print(f"  CVR: {result['cvr_diagnosis']['diagnosed_cause']}")
        print(f"  CPC: {result['cpc_diagnosis']['diagnosed_cause']}")
        if result["should_do_nothing"]:
            print(f"  → DO NOTHING: {result['do_nothing_triggers']}")
