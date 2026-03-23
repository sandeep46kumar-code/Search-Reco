"""
outcome_tracker.py
Cron job: runs daily at 10am.
Finds implementations confirmed > 7 days ago, measures KPI delta, saves outcome.
"""
import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from memory import (
    get_pending_outcomes, save_outcome,
    recompute_accuracy_scores, get_conn
)
from data_pull import pull_campaign_data
from normalizer import normalize


def _improved(kpi_delta: float, kpi: str) -> bool:
    """True if the KPI delta represents improvement."""
    if kpi in ("CPA", "CPL", "CPC_CAP"):
        return kpi_delta < -0.03   # CPA fell > 3%
    elif kpi == "ROAS":
        return kpi_delta > 0.03    # ROAS rose > 3%
    elif kpi == "VOLUME":
        return kpi_delta > 0.05    # Conversions up > 5%
    return kpi_delta < 0


def run_outcome_analysis():
    print(f"\n{'='*50}")
    print(f"  Outcome Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}\n")

    pending = get_pending_outcomes()
    print(f"→ {len(pending)} implementations awaiting 7-day outcome check")

    if not pending:
        print("  Nothing to update.")
        return

    # Pull current data once
    customer_id = os.getenv("CUSTOMER_ID", "demo")
    try:
        df      = pull_campaign_data(customer_id, days=14)
        signals = normalize(df)
        signal_map = {s["name"]: s for s in signals}
    except Exception as e:
        print(f"⚠ Could not pull current data: {e}")
        signal_map = {}

    updated = 0
    for impl in pending:
        campaign = impl.get("campaign", "")
        t0       = impl.get("t0_snapshot") or {}

        # Parse T0 snapshot
        if isinstance(t0, str):
            try:
                t0 = json.loads(t0)
            except Exception:
                t0 = {}

        kpi_before = float(t0.get("primary_metric", 0))
        kpi        = t0.get("kpi", "CPA")

        # Get current metric
        current_signal = signal_map.get(campaign, {})
        kpi_after = current_signal.get("primary_metric_7d") or kpi_before

        if kpi_before > 0:
            if kpi in ("ROAS", "VOLUME"):
                delta = (kpi_after - kpi_before) / kpi_before
            else:
                delta = (kpi_before - kpi_after) / kpi_before  # positive = improvement
        else:
            delta = 0.0

        is_improved = _improved(delta, kpi)

        # Check if diagnosed cause actually changed
        t0_cause = t0.get("diagnosed_cause", "")
        cause_validated = bool(t0_cause and is_improved)

        outcome = "POSITIVE" if is_improved else ("NEGATIVE" if abs(delta) > 0.03 else "NEUTRAL")

        t7_snapshot = {
            "primary_metric": round(kpi_after, 2),
            "kpi":            kpi,
            "measured_at":    datetime.now().isoformat(),
        }

        save_outcome(
            impl_id         = impl["id"],
            t7_snapshot     = t7_snapshot,
            kpi_delta       = round(delta, 4),
            outcome         = outcome,
            cause_validated = cause_validated
        )

        print(f"  {'✓' if is_improved else '✗'} {campaign[:40]:40} "
              f"| {kpi} delta: {delta:+.1%} | {outcome}")
        updated += 1

    print(f"\n→ Recomputing accuracy scores...")
    recompute_accuracy_scores()

    print(f"✓ Outcome analysis complete — {updated} implementations processed.\n")


if __name__ == "__main__":
    import sys
    try:
        run_outcome_analysis()
        sys.exit(0)
    except Exception as e:
        print(f"FATAL: outcome_tracker failed: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
