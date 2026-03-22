"""
stability.py
Layer 3b — Data Stability & Weighting Engine.

Analyzes day-level data within the fully-attributed evaluation window to:
- Detect outlier days (CPA / CVR / CPC spikes)
- Detect query mix shifts
- Apply weighted averages (down-weight anomalies)
- Output Stability flag: STABLE / MODERATE / VOLATILE
"""
import math
from datetime import date, timedelta
from typing import Optional


# ── ATTRIBUTION CONFIG ────────────────────────────────────────────────────────

DEFAULT_ATTRIBUTION_WINDOW = 14   # days
RECENCY_WEIGHT = 0.30             # weight given to last-N days (trend only)
PRIMARY_WEIGHT = 1.0              # weight for fully attributed window

OUTLIER_THRESHOLD_STD = 2.0       # days beyond 2 std devs are flagged
MIN_DAYS_FOR_STABILITY = 7        # need at least 7 days to assess stability


# ── DATE WINDOWS ──────────────────────────────────────────────────────────────

def get_attribution_windows(attribution_n: int = DEFAULT_ATTRIBUTION_WINDOW) -> dict:
    """
    Returns the two data windows:
    - primary: Day-(N+30) to Day-N  (fully matured, full weight)
    - recency: Last N days           (partial attribution, 30% weight, trend only)
    """
    today = date.today()
    primary_end   = today - timedelta(days=attribution_n)
    primary_start = today - timedelta(days=attribution_n + 30)
    recency_start = today - timedelta(days=attribution_n - 1)
    recency_end   = today - timedelta(days=1)

    return {
        "attribution_n":   attribution_n,
        "primary_start":   primary_start,
        "primary_end":     primary_end,
        "recency_start":   recency_start,
        "recency_end":     recency_end,
        "primary_label":   f"{primary_start.strftime('%d %b')} – {primary_end.strftime('%d %b %Y')} (30d fully attributed)",
        "recency_label":   f"{recency_start.strftime('%d %b')} – {recency_end.strftime('%d %b %Y')} (last {attribution_n}d, trend only)",
        "full_label":      (f"{primary_start.strftime('%d %b')} – {recency_end.strftime('%d %b %Y')} "
                           f"| N={attribution_n}d attribution window"),
    }


# ── OUTLIER DETECTION ─────────────────────────────────────────────────────────

def detect_outlier_days(daily_values: list[float],
                         threshold_std: float = OUTLIER_THRESHOLD_STD) -> dict:
    """
    Given a list of daily values (CPA, CVR, or CPC), detect outlier days.
    Returns outlier indices, mean, std, and suggested weights.
    """
    if len(daily_values) < 3:
        return {"outlier_indices": [], "mean": 0, "std": 0,
                "weights": [1.0] * len(daily_values)}

    n = len(daily_values)
    mean = sum(daily_values) / n
    variance = sum((x - mean) ** 2 for x in daily_values) / n
    std = math.sqrt(variance) if variance > 0 else 0

    outlier_indices = []
    weights = []
    for i, v in enumerate(daily_values):
        if std > 0 and abs(v - mean) > threshold_std * std:
            outlier_indices.append(i)
            # Down-weight outliers: inversely proportional to deviation
            deviation = abs(v - mean) / std
            weights.append(max(0.1, 1.0 / deviation))
        else:
            weights.append(1.0)

    return {
        "outlier_indices":   outlier_indices,
        "outlier_count":     len(outlier_indices),
        "mean":              round(mean, 4),
        "std":               round(std, 4),
        "weights":           weights,
        "outlier_influence": round(len(outlier_indices) / n * 100, 1) if n > 0 else 0,
    }


def weighted_mean(values: list[float], weights: list[float]) -> float:
    """Compute a weighted mean. Returns 0 if weights sum to 0."""
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total_weight


# ── STABILITY CLASSIFICATION ──────────────────────────────────────────────────

def classify_stability(cv: float, outlier_pct: float) -> str:
    """
    Classify data stability based on coefficient of variation and outlier %.
    cv = std / mean (coefficient of variation)
    """
    if cv < 0.20 and outlier_pct < 10:
        return "STABLE"
    elif cv < 0.40 and outlier_pct < 25:
        return "MODERATE"
    else:
        return "VOLATILE"


# ── FULL STABILITY ANALYSIS ───────────────────────────────────────────────────

def analyze_stability(daily_metrics: list[dict],
                      kpi: str = "CPA",
                      attribution_n: int = DEFAULT_ATTRIBUTION_WINDOW) -> dict:
    """
    Full stability analysis for a campaign's daily data.

    daily_metrics: list of dicts with keys: date, cost, conversions, clicks,
                   conversion_value, impressions

    Returns:
        stability_flag:     STABLE | MODERATE | VOLATILE
        weighted_cpa:       CPA computed with outlier down-weighting
        raw_cpa:            Simple mean CPA
        cpa_volatility:     Coefficient of variation
        outlier_days:       Count of anomalous days
        dominant_days:      Days that disproportionately influence result
        recency_trend:      IMPROVING | WORSENING | STABLE (from last N days)
        recommendation:     Human-readable stability note
        windows:            Attribution window metadata
    """
    windows = get_attribution_windows(attribution_n)

    if not daily_metrics or len(daily_metrics) < MIN_DAYS_FOR_STABILITY:
        return {
            "stability_flag":  "LOW_DATA",
            "weighted_cpa":    None,
            "raw_cpa":         None,
            "cpa_volatility":  None,
            "outlier_days":    0,
            "dominant_days":   [],
            "recency_trend":   "UNKNOWN",
            "recommendation":  f"Fewer than {MIN_DAYS_FOR_STABILITY} days of data in the fully attributed window.",
            "windows":         windows,
        }

    # Compute daily CPA / ROAS / CVR depending on KPI
    daily_cpas = []
    for row in daily_metrics:
        cost = float(row.get("cost", 0) or 0)
        conv = float(row.get("conversions", 0) or 0)
        clicks = float(row.get("clicks", 0) or 0)
        cv = float(row.get("conversion_value", 0) or 0)

        if kpi in ("CPA", "CPL"):
            metric = cost / conv if conv > 0 else None
        elif kpi == "ROAS":
            metric = cv / cost if cost > 0 else None
        elif kpi == "CPC_CAP":
            metric = cost / clicks if clicks > 0 else None
        else:
            metric = cost / conv if conv > 0 else None

        if metric is not None:
            daily_cpas.append(metric)

    if len(daily_cpas) < MIN_DAYS_FOR_STABILITY:
        return {
            "stability_flag":  "LOW_DATA",
            "weighted_cpa":    None,
            "raw_cpa":         None,
            "cpa_volatility":  None,
            "outlier_days":    0,
            "dominant_days":   [],
            "recency_trend":   "UNKNOWN",
            "recommendation":  "Not enough conversion days for stability analysis.",
            "windows":         windows,
        }

    outlier_result = detect_outlier_days(daily_cpas)
    weights        = outlier_result["weights"]
    raw_cpa        = outlier_result["mean"]
    weighted_cpa   = weighted_mean(daily_cpas, weights)
    std            = outlier_result["std"]
    cv             = std / raw_cpa if raw_cpa > 0 else 0
    outlier_pct    = outlier_result["outlier_influence"]

    stability_flag = classify_stability(cv, outlier_pct)

    # Identify dominant days (top 20% of weight influence)
    total_w    = sum(weights)
    day_shares = [(i, w / total_w * 100) for i, w in enumerate(weights)]
    day_shares.sort(key=lambda x: -x[1])
    dominant_days = [
        {"day_index": i, "share_pct": round(pct, 1)}
        for i, pct in day_shares
        if pct > 20 and i in outlier_result["outlier_indices"]
    ]

    # Recency trend: compare last 7 days vs prior 7 days of the primary window
    recency_trend = "STABLE"
    if len(daily_cpas) >= 14:
        mid   = len(daily_cpas) // 2
        first = sum(daily_cpas[:mid]) / mid
        last  = sum(daily_cpas[mid:]) / (len(daily_cpas) - mid)
        delta = (last - first) / first if first > 0 else 0
        if kpi in ("CPA", "CPL", "CPC_CAP"):
            recency_trend = "WORSENING" if delta > 0.10 else ("IMPROVING" if delta < -0.10 else "STABLE")
        else:  # ROAS, VOLUME — higher is better
            recency_trend = "IMPROVING" if delta > 0.10 else ("WORSENING" if delta < -0.10 else "STABLE")

    # Human-readable recommendation
    if stability_flag == "STABLE":
        rec_note = "Data is stable — high confidence in recommendations."
    elif stability_flag == "MODERATE":
        rec_note = (f"Moderate volatility (CV={cv:.0%}). "
                    f"{outlier_result['outlier_count']} outlier day(s) down-weighted. "
                    "Recommendations are valid but treat as medium confidence.")
    else:
        rec_note = (f"High volatility (CV={cv:.0%}, {outlier_pct:.0f}% of spend in outlier days). "
                    "Weighted averages applied. Consider waiting for more stable data before acting.")

    return {
        "stability_flag":  stability_flag,
        "weighted_cpa":    round(weighted_cpa, 2) if weighted_cpa else None,
        "raw_cpa":         round(raw_cpa, 2),
        "cpa_delta_pct":   round((weighted_cpa - raw_cpa) / raw_cpa * 100, 1) if raw_cpa > 0 else 0,
        "cpa_volatility":  round(cv, 3),
        "outlier_days":    outlier_result["outlier_count"],
        "outlier_indices": outlier_result["outlier_indices"],
        "dominant_days":   dominant_days,
        "recency_trend":   recency_trend,
        "recommendation":  rec_note,
        "windows":         windows,
        "days_analyzed":   len(daily_cpas),
    }


# ── BLENDED SIGNAL ────────────────────────────────────────────────────────────

def blend_primary_recency(primary_metric: float,
                           recency_metric: Optional[float],
                           recency_weight: float = RECENCY_WEIGHT) -> dict:
    """
    Blend fully-attributed primary metric with recency signal.
    Primary gets (1 - recency_weight) weight.
    Recency is used ONLY for trend direction, not absolute CPA.
    """
    if recency_metric is None:
        return {
            "blended_metric": primary_metric,
            "trend_signal":   "NO_RECENCY_DATA",
            "recency_used":   False,
        }

    primary_w = 1.0 - recency_weight
    blended   = primary_metric * primary_w + recency_metric * recency_weight

    # Trend from recency vs primary
    delta = (recency_metric - primary_metric) / primary_metric if primary_metric > 0 else 0
    if abs(delta) < 0.05:
        trend = "CONSISTENT"
    elif delta > 0.15:
        trend = "RECENT_DETERIORATION"
    elif delta < -0.15:
        trend = "RECENT_IMPROVEMENT"
    else:
        trend = "SLIGHT_SHIFT"

    return {
        "blended_metric": round(blended, 2),
        "primary_metric": round(primary_metric, 2),
        "recency_metric": round(recency_metric, 2),
        "recency_delta":  round(delta * 100, 1),
        "trend_signal":   trend,
        "recency_used":   True,
        "recency_weight": recency_weight,
    }


if __name__ == "__main__":
    # Test with synthetic volatile data
    import random
    random.seed(42)
    daily = []
    for i in range(30):
        cost = random.uniform(8000, 15000)
        conv = random.uniform(30, 80)
        # Inject 2 spike days
        if i in (7, 22):
            conv = 5  # massive CPA spike
        daily.append({"cost": cost, "conversions": conv})

    result = analyze_stability(daily, kpi="CPA", attribution_n=14)
    print(f"Stability:    {result['stability_flag']}")
    print(f"Raw CPA:      Rs {result['raw_cpa']:,.0f}")
    print(f"Weighted CPA: Rs {result['weighted_cpa']:,.0f}")
    print(f"Outlier days: {result['outlier_days']}")
    print(f"Recency trend:{result['recency_trend']}")
    print(f"Note:         {result['recommendation']}")
    print(f"Windows:      {result['windows']['primary_label']}")
