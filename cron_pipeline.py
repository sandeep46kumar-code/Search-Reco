"""
cron_pipeline.py
Daily cron entrypoint — runs the full recommendation pipeline.
Railway cron start command: python cron_pipeline.py
Schedule: 0 9 * * * (9am IST = 3:30am UTC)
"""
import os
import sys
from datetime import datetime

print(f"\n{'='*50}")
print(f"  Pipeline Cron — {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
print(f"{'='*50}\n")

try:
    from pipeline import run

    customer_id   = os.getenv("CUSTOMER_ID", "demo")
    target_cpa    = float(os.getenv("DEFAULT_TARGET_CPA", 250))
    attribution_n = int(os.getenv("ATTRIBUTION_WINDOW_DAYS", 14))

    print(f"  Customer:     {customer_id}")
    print(f"  Target CPA:   Rs {target_cpa}")
    print(f"  Attribution:  N={attribution_n} days\n")

    results = run(
        customer_id=customer_id,
        target_cpa=target_cpa,
        dry_run=False,
        attribution_n=attribution_n,
    )

    stats = results.get("stats", {})
    print(f"\n✓ Pipeline complete:")
    print(f"  Campaigns processed: {stats.get('campaigns_total', 0)}")
    print(f"  Recommendations:     {stats.get('recs_approved', 0)} approved, "
          f"{stats.get('recs_rejected', 0)} rejected")
    print(f"  Duration:            {stats.get('duration_secs', 0):.1f}s")
    sys.exit(0)

except Exception as e:
    print(f"\nFATAL: Pipeline cron failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
