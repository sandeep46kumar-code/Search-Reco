"""
pipeline.py
Main orchestrator. Runs all 8 layers in sequence.
Usage: python pipeline.py [--dry-run] [--campaigns C001,C002]
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from memory import (
    setup_tables, get_all_active_configs, get_accuracy_scores,
    get_memory_context, save_recommendations, check_change_budget, log_run
)
from data_pull import pull_campaign_data, pull_search_term_data
from normalizer import normalize
from query_engine import build_query_signals, summarise_query_signals
from cause_diagnosis import run_all_diagnoses
from intelligence import run_intelligence
from copy_intelligence import detect_brand_over_index, check_incrementality
from portfolio import compute_portfolio_impact
from lever_scorer import score_levers
from agents import run_analyst, run_rec_engine, run_gatekeeper
from stability import analyze_stability, get_attribution_windows


def run(customer_id: str = None,
        target_cpa: float = None,
        dry_run: bool = False,
        scope: list[str] = None,
        attribution_n: int = None) -> dict:

    start_time = time.time()
    # customer_id from API call takes priority over env var default
    customer_id  = customer_id if customer_id and customer_id != "demo" else os.getenv("CUSTOMER_ID", customer_id or "demo")
    target_cpa   = target_cpa  or float(os.getenv("DEFAULT_TARGET_CPA", 250))
    attribution_n = attribution_n or int(os.getenv("ATTRIBUTION_WINDOW_DAYS", 14))
    windows       = get_attribution_windows(attribution_n)

    print(f"\n{'='*60}")
    print(f"  Recommendation Engine Pipeline")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Customer: {customer_id}")
    print(f"  Attribution N={attribution_n}d | Primary window: {windows['primary_label']}")
    print(f"  Recency window: {windows['recency_label']} (30% weight, trend only)")
    print(f"{'='*60}\n")

    # ── SETUP ────────────────────────────────────────────────────────────────
    print("→ [0/8] Setting up database tables...")
    setup_tables()

    # ── L0: CONFIG ───────────────────────────────────────────────────────────
    print("→ [L0] Loading campaign configs...")
    configs = get_all_active_configs()
    if scope:
        configs = [c for c in configs if c["campaign_id"] in scope]
    print(f"   {len(configs)} active configs loaded.")

    # ── DATA PULL ─────────────────────────────────────────────────────────────
    print("→ [DATA] Pulling Google Ads data...")
    # Pull TWO separate windows — this is the actual implementation of blended attribution
    print(f"→ [L0] Pulling PRIMARY window: {windows['primary_label']}")
    campaign_df = pull_campaign_data(
        customer_id,
        start_date=windows["primary_start"],
        end_date=windows["primary_end"]
    )

    print(f"→ [L0] Pulling RECENCY window: {windows['recency_label']}")
    recency_df = pull_campaign_data(
        customer_id,
        start_date=windows["recency_start"],
        end_date=windows["recency_end"]
    )

    search_term_df = pull_search_term_data(customer_id)
    print(f"   {len(campaign_df)} campaigns | {len(search_term_df)} search terms")

    # ── NORMALISE ─────────────────────────────────────────────────────────────
    print("→ [NORM] Normalising schema...")
    signals = normalize(campaign_df, configs, attribution_n=attribution_n, recency_df=recency_df)
    if scope:
        signals = [s for s in signals if s["campaign_id"] in scope or s["name"] in scope]

    # Run stability analysis per campaign
    print("→ [L3b] Running stability analysis...")
    import random as _rand; _rand.seed(42)
    for s in signals:
        daily_proxy = []
        for _ in range(20):
            noise = 1 + _rand.gauss(0, s.get("cpa_volatility", 0.1))
            m = s.get("primary_metric_7d") or target_cpa
            daily_proxy.append({"cost": 10000, "conversions": max(1, 10000 / (m * max(0.1, noise)))})
        stab = analyze_stability(daily_proxy, kpi=s.get("kpi","CPA"), attribution_n=attribution_n)
        s["stability_flag"]  = stab["stability_flag"]
        s["weighted_metric"] = stab["weighted_cpa"]
        s["recency_trend"]   = stab["recency_trend"]
        s["stability_note"]  = stab["recommendation"]
        s["data_window"]     = windows["primary_label"]
        s["attribution_n"]   = attribution_n

    sufficient = [s for s in signals if s["confidence"] == "SUFFICIENT"]
    unstable   = [s for s in signals if s.get("unstable")]
    print(f"   {len(signals)} campaigns | {len(sufficient)} sufficient | {len(unstable)} unstable")

    # ── L1: QUERY ENGINE ─────────────────────────────────────────────────────
    print("→ [L1] Running query engine...")
    search_rows   = search_term_df.to_dict("records")
    query_signals = build_query_signals(search_rows)
    query_summary = summarise_query_signals(query_signals)
    print(f"   {query_summary['total_queries']} queries | "
          f"waste cost Rs {query_summary['zero_conv_cost']:,.0f} "
          f"({query_summary['zero_conv_pct']}%) | "
          f"{len(query_summary['duplicate_queries'])} dupes")

    # ── L2: CAUSE DIAGNOSIS ───────────────────────────────────────────────────
    print("→ [L2] Running cause diagnosis...")
    diagnoses = [run_all_diagnoses(s, query_signals, signals) for s in signals]
    do_nothing_count = sum(1 for d in diagnoses if d["should_do_nothing"])
    print(f"   {do_nothing_count}/{len(diagnoses)} campaigns → DO_NOTHING")

    # ── L3: INTELLIGENCE ──────────────────────────────────────────────────────
    print("→ [L3] Running marginal analysis...")
    intel = run_intelligence(signals)
    print(f"   {len(intel['spend_tiers'])} spend tiers computed")

    # ── L4: COPY INTELLIGENCE ─────────────────────────────────────────────────
    print("→ [L4] Running copy intelligence...")
    for s in signals:
        s["incrementality_flag"] = check_incrementality(s)

    # ── L5: PORTFOLIO ─────────────────────────────────────────────────────────
    print("→ [L5] Computing portfolio state...")
    # (portfolio checks applied per-rec during generation)

    # ── L6: LEVER SCORING ────────────────────────────────────────────────────
    print("→ [L6] Scoring levers...")
    accuracy      = get_accuracy_scores()
    lever_scores  = {}
    for s in signals:
        scores = score_levers(s, accuracy, query_summary)
        lever_scores[s["name"]] = scores
    active_levers = sum(len(v) for v in lever_scores.values())
    print(f"   {active_levers} active lever scores across {len(signals)} campaigns")

    # ── CHANGE BUDGETS ────────────────────────────────────────────────────────
    change_budgets = {}
    for s in signals:
        max_actions = s.get("max_actions_per_day", 2)
        change_budgets[s["name"]] = check_change_budget(s["campaign_id"], max_actions)

    # ── L7: RECOMMENDATION GENERATION ────────────────────────────────────────
    print("→ [L7] Generating recommendations (Claude API call 1 + 2)...")
    memory = get_memory_context()

    # Pass cpa_band into memory context so analyst can reference it
    memory["cpa_band"] = intel.get("cpa_band")

    # Enrich signals with dual CPA fields from intelligence pass
    intel_map = {c["name"]: c for c in intel.get("campaign_elasticity", [])}
    for s in signals:
        ic = intel_map.get(s["name"], {})
        s["efficiency_regime"]  = ic.get("efficiency_regime", "UNKNOWN")
        s["hard_target_status"] = ic.get("hard_target_status", "UNKNOWN")
        s["vs_peers"]           = ic.get("vs_peers")
        s["decision_basis"]     = ic.get("decision_basis")

    analyst_output = run_analyst(signals, target_cpa, memory)
    print(f"   Analyst enriched {len(analyst_output)} campaigns")

    raw_recs = run_rec_engine(
        analyst_output, signals, diagnoses,
        lever_scores, query_summary, target_cpa,
        memory, change_budgets, intel=intel
    )
    print(f"   Engine generated {len(raw_recs)} recommendations")

    # ── PORTFOLIO FILTER ──────────────────────────────────────────────────────
    for rec in raw_recs:
        if rec.get("type") != "DO_NOTHING":
            pi = compute_portfolio_impact(rec, signals, query_signals)
            # Sanitize Infinity values before storing in rec
            import math
            def _san(o):
                if isinstance(o, float) and (math.isnan(o) or math.isinf(o)): return None
                if isinstance(o, dict): return {k: _san(v) for k,v in o.items()}
                if isinstance(o, list): return [_san(v) for v in o]
                return o
            rec["portfolio_impact"] = _san(pi)
            if pi["portfolio_verdict"] == "BLOCKED":
                rec["pre_blocked"] = True
                rec["pre_block_reason"] = pi["blocked_reasons"]

    # ── L8: GATEKEEPER ────────────────────────────────────────────────────────
    print("→ [L8] Running strategic gatekeeper (Claude API call 3)...")
    validation = run_gatekeeper(raw_recs, signals, target_cpa, change_budgets)

    # Merge validation results
    final_recs = []
    for i, rec in enumerate(raw_recs):
        v = next((x for x in validation if x.get("index") == i),
                 {"verdict": "REJECTED", "reason": "Validation result missing"})

        # Portfolio pre-block overrides gatekeeper approval
        if rec.get("pre_blocked"):
            v["verdict"]  = "REJECTED"
            v["reason"]   = f"Portfolio blocked: {rec.get('pre_block_reason', '')}"

        camp_signal = next((s for s in signals if s["name"] == rec.get("campaign")), {})
        final_rec = {
            **rec,
            "verdict":              v["verdict"],
            "rejection_reason":     v.get("reason"),
            "checks_failed":        v.get("checks_failed", []),
            "cpa_before":           camp_signal.get("primary_metric_7d") or camp_signal.get("primary_metric_14d"),
            "cpa_target_used":      camp_signal.get("target_value", target_cpa),
            "attribution_window":   f"N={attribution_n} days",
            "data_window_used":     windows["primary_label"],
            "recency_window":       windows["recency_label"],
            "stability_flag":       camp_signal.get("stability_flag", "UNKNOWN"),
            "recency_conflict":     camp_signal.get("recency_conflict", False),
            "recency_flag":         camp_signal.get("recency_flag", "NO_RECENCY_DATA"),
            "recency_delta_pct":    camp_signal.get("recency_delta_pct"),
            "recency_note":         camp_signal.get("recency_note", ""),
            "recency_trend":        camp_signal.get("recency_trend", "UNKNOWN"),
            "observation_window":   windows["full_label"],
        }
        final_recs.append(final_rec)

    approved = [r for r in final_recs if r["verdict"] == "APPROVED"]
    rejected = [r for r in final_recs if r["verdict"] == "REJECTED"]
    do_nothing = [r for r in final_recs if r.get("type") == "DO_NOTHING"]

    print(f"\n{'─'*60}")
    print(f"  APPROVED: {len(approved)} | REJECTED: {len(rejected)} | DO_NOTHING: {len(do_nothing)}")
    print(f"{'─'*60}\n")

    for r in approved:
        print(f"  ✓ [{r.get('type','').upper()[:20]:20}] {r.get('action','')[:70]}")
    for r in rejected:
        print(f"  ✗ [{r.get('type','').upper()[:20]:20}] {r.get('rejection_reason','')[:70]}")

    duration = round(time.time() - start_time, 1)

    if not dry_run:
        print(f"\n→ Saving {len(final_recs)} recommendations to database...")
        ids = save_recommendations(final_recs)
        log_run({
            "campaigns_total": len(signals),
            "sufficient":      len(sufficient),
            "recs_generated":  len(final_recs),
            "recs_approved":   len(approved),
            "recs_rejected":   len(rejected),
            "duration_secs":   duration,
        })
        print(f"   Saved with IDs: {ids}")
    else:
        print("\n[DRY RUN] — nothing saved to database.")

    print(f"\n✓ Pipeline complete in {duration}s\n")

    return {
        "approved":     approved,
        "rejected":     rejected,
        "do_nothing":   do_nothing,
        "all":          final_recs,
        "query_summary":query_summary,
        "duration":     duration,
        "stats": {
            "campaigns_total": len(signals),
            "sufficient":      len(sufficient),
            "recs_approved":   len(approved),
            "recs_rejected":   len(rejected),
        }
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Recommendation Engine pipeline")
    parser.add_argument("--dry-run",    action="store_true", help="Don't save to DB")
    parser.add_argument("--campaigns",  type=str, help="Comma-separated campaign IDs to scope")
    parser.add_argument("--target-cpa", type=float, default=None)
    args = parser.parse_args()

    scope = args.campaigns.split(",") if args.campaigns else None
    results = run(
        target_cpa=args.target_cpa,
        dry_run=args.dry_run,
        scope=scope
    )
    sys.exit(0)
