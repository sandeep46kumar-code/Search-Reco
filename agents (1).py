"""
agents.py
The three Claude API calls: Analyst · Recommendation Engine · Gatekeeper.
Each is a tightly scoped system prompt with structured JSON output.
"""
import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

ANALYST_MODEL    = os.getenv("ANALYST_MODEL",    "claude-haiku-4-5-20251001")
ENGINE_MODEL     = os.getenv("ENGINE_MODEL",     "claude-opus-4-5")
GATEKEEPER_MODEL = os.getenv("GATEKEEPER_MODEL", "claude-opus-4-5")


def _call(system: str, user: str, model: str, max_tokens: int = 2000) -> str:
    """Single Claude API call. Returns raw text response."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return response.content[0].text


def _parse_json(raw: str) -> list | dict:
    """Strip markdown fences and parse JSON."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    return json.loads(cleaned)


# ── AGENT 1: ANALYST ─────────────────────────────────────────────────────────

ANALYST_SYSTEM = """You are a Google Ads performance analyst specialising in marginal CPA analysis.
You receive structured campaign signals and return ONLY a JSON array of enriched signal objects.

Each object must have exactly these fields:
{
  "name": "campaign name",
  "classification": "SCALABLE|SATURATION|DIMINISHING|LOW_CONFIDENCE",
  "key_insight": "one sentence citing specific numbers",
  "elasticity_action": "budget|bids|none",
  "confidence": "High|Medium|Low",
  "top_lever": "the single highest priority lever for this campaign"
}

Rules:
- key_insight MUST contain at least one number (Rs, %, or count)
- Never invent numbers not present in the input
- Return ONLY valid JSON array, no markdown, no explanation"""


def run_analyst(signals: list[dict], target_cpa: float, memory: dict) -> list[dict]:
    """Agent 1: Analyse campaign signals and enrich with insights."""
    sufficient = [
        s for s in signals
        if s["confidence"] == "SUFFICIENT" and not s.get("unstable")
    ]
    if not sufficient:
        return []

    band = memory.get("cpa_band")
    band_str = band["description"] if band else "insufficient data for band"
    user = f"""Observation window: {_date_window()}
Account CPA distribution band: {band_str}
Hard target CPA: Rs {target_cpa}

NOTE: Every insight must state whether the recommendation basis is:
  (A) Fixed hard target (Rs {target_cpa}) OR
  (B) Relative efficiency vs peer distribution (the band above)

Campaign signals ({len(sufficient)} sufficient):
{json.dumps([{
    "name": s["name"],
    "kpi": s["kpi"],
    "classification": s["classification"],
    "primary_metric_7d": s["primary_metric_7d"],
    "target_value": s["target_value"],
    "is_lost_budget": s["is_lost_budget"],
    "is_lost_rank": s["is_lost_rank"],
    "is_abs_top": s["is_abs_top"],
    "ctr": s["ctr"],
    "cvr": s["cvr"],
    "cpa_volatility": s["cpa_volatility"],
        "efficiency_regime":  s.get("efficiency_regime", "UNKNOWN"),
        "hard_target_status": s.get("hard_target_status", "UNKNOWN"),
        "vs_peers":           s.get("vs_peers"),
        "decision_basis":     s.get("decision_basis"),
} for s in sufficient], indent=2)}

Historical accuracy (lever -> accuracy score):
{json.dumps(memory.get("accuracy_scores", {}), indent=2)}

Campaigns where human previously rejected recommendations (avoid repeating):
{json.dumps(memory.get("repeat_rejects", []), indent=2)}"""

    try:
        raw = _call(ANALYST_SYSTEM, user, ANALYST_MODEL)
        return _parse_json(raw)
    except Exception as e:
        print(f"⚠ Analyst API error: {e} — using fallback.")
        return _analyst_fallback(sufficient, target_cpa)


def _analyst_fallback(signals: list[dict], target_cpa: float) -> list[dict]:
    """Deterministic fallback when API is unavailable."""
    result = []
    for s in signals:
        m = s.get("primary_metric_7d") or target_cpa
        action = "none"
        if s["is_lost_budget"] > 0.2:
            action = "budget"
        elif s["is_lost_rank"] > 0.2:
            action = "bids"
        result.append({
            "name": s["name"],
            "classification": s["classification"],
            "key_insight": (
                f"CPA Rs {m:.0f} vs target Rs {target_cpa:.0f}. "
                f"IS lost (budget) {s['is_lost_budget']:.0%}."
            ),
            "elasticity_action": action,
            "confidence": "Medium",
            "top_lever": "budget_reallocation" if action == "budget" else "bid_adjustment"
        })
    return result


# ── AGENT 2: RECOMMENDATION ENGINE ───────────────────────────────────────────

ENGINE_SYSTEM = """You are a Google Ads recommendation engine for a Paytm-scale Search account.
You receive signals from 6 upstream analysis layers and generate recommendations.

CRITICAL RULES:
1. DO_NOTHING is a valid and preferred output when evidence is weak
2. Never recommend scaling a DIMINISHING campaign
3. Every action MUST include a specific Rs amount or % — no vague language
4. Never recommend copy changes without position-adjusted evidence
5. RECENCY CONFLICT RULE: if a signal has recency_conflict=True, you MUST:
   - Still use primary_metric (not recency) for CPA calculation
   - Set confidence to "Low" regardless of other factors
   - Include the recency_note verbatim in your "why" field
   - Add "Verify recency trend before acting" to risk_if_ignored
5. Only generate recommendations where a cause has been diagnosed
6. Respect change_budget — omit recs for campaigns with 0 remaining budget

Output: JSON array only. Each object:
{
  "type": "budget_reallocation|bid_adjustment|ad_copy|search_term_mining|match_type|landing_page|audience_layering|dayparting|device_reallocation|quality_score|DO_NOTHING",
  "campaign": "campaign name",
  "segment": "brand|generic|competitor",
  "intent_cluster": "transactional|comparative|informational|price_seeking|navigational|unclassified",
  "cause": { "metric": "", "diagnosed_cause": "", "evidence": "" },
  "action": "specific implementable action with Rs or % amount",
  "why": "data-backed reasoning with specific numbers",
  "kpi_delta_estimate": "expected change e.g. CPA -8% or CVR +12%",
  "observation_window": "e.g. 15 Mar – 21 Mar 2026 (7d) vs 08 Mar – 21 Mar 2026 (14d baseline)",
  "attribution_window_used": "e.g. N=14 days — data from 06 Feb to 08 Mar 2026",
  "data_window_used": "exact date range used for this recommendation",
  "cpa_target_used": 250,
  "stability_flag": "STABLE | MODERATE | VOLATILE | LOW_DATA",
  "recency_trend": "IMPROVING | WORSENING | CONSISTENT | SLIGHT_SHIFT",
  "risk_if_ignored": "specific consequence of not acting, quantified where possible"
  "est_conv_delta": 0,
  "lever": "lever name",
  "priority_rank": 1,
  "change_cost": 1,
  "confidence": "High|Medium|Low",
  "incrementality_flag": "GROWTH|HARVESTING|NEUTRAL"
}

For DO_NOTHING: include type=DO_NOTHING, campaign, reason (string), and monitor (array of metrics to watch)."""


def _date_window() -> str:
    """Return human-readable observation window for the current run."""
    from datetime import date, timedelta
    today = date.today()
    end = today - timedelta(days=1)
    start_7d  = end - timedelta(days=6)
    start_14d = end - timedelta(days=13)
    return (f"7d primary window: {start_7d.strftime('%d %b')} – {end.strftime('%d %b %Y')} | "
            f"14d baseline: {start_14d.strftime('%d %b')} – {end.strftime('%d %b %Y')}")


def run_rec_engine(analyst_output: list[dict],
                   signals: list[dict],
                   diagnoses: list[dict],
                   lever_scores: dict,
                   query_summary: dict,
                   target_cpa: float,
                   memory: dict,
                   change_budgets: dict,
                   intel: dict = None) -> list[dict]:
    """Agent 2: Generate recommendations from all upstream signals."""

    # Build compact signal context
    _attr_n = attribution_n  # alias for f-string
    # Build signal context — include recency conflict so Claude knows to flag it
    signal_context = {s["name"]: {
        "classification": s["classification"],
        "cpa_7d": s.get("primary_metric_7d"),
        "target": s.get("target_value"),
        "kpi": s.get("kpi"),
        "confidence": s["confidence"],
        "change_budget": change_budgets.get(s["name"], {}).get("remaining", 2),
    } for s in signals}

    user = f"""=== ATTRIBUTION & STABILITY CONFIG ===
Observation window: {_date_window()}
Attribution window (N): {attribution_n} days — EXCLUDE last {attribution_n} days from primary analysis
Primary data window: {_date_window().split("|")[0].strip()}
Recency signal: Last {attribution_n} days at 30% weight — use for TREND DIRECTION ONLY, never absolute CPA
Target CPA: Rs {target_cpa}

Analyst insights:
{json.dumps(analyst_output, indent=2)}

Cause diagnoses:
{json.dumps([{
    "campaign": d["campaign"],
    "ctr": d["ctr_diagnosis"]["diagnosed_cause"],
    "cvr": d["cvr_diagnosis"]["diagnosed_cause"],
    "cpc": d["cpc_diagnosis"]["diagnosed_cause"],
    "do_nothing": d["should_do_nothing"],
    "triggers": d["do_nothing_triggers"]
} for d in diagnoses], indent=2)}

Top lever scores per campaign:
{json.dumps({k: list(v.items())[:3] for k, v in lever_scores.items()}, indent=2)}

Account CPA Band:
{json.dumps(intel.get("cpa_band") if intel else {}, indent=2) if False else json.dumps({"band": next((c.get("vs_peers") for c in intel.get("campaign_elasticity",[]) if c.get("vs_peers")), "N/A") if intel else "N/A"})}

Query-level summary:
{json.dumps({
    "zero_conv_cost_pct": query_summary.get("zero_conv_pct"),
    "top_waste_patterns": [w["ngram"] for w in query_summary.get("waste_patterns", [])[:5]],
    "duplicate_queries": len(query_summary.get("duplicate_queries", [])),
    "segment_breakdown": query_summary.get("segment_breakdown"),
}, indent=2)}

All campaign signals:
{json.dumps(signal_context, indent=2)}

Recent outcomes from memory:
{json.dumps(memory.get("recent_approved", [])[-5:], indent=2)}

Campaigns to NOT repeat same rec type (human rejected before):
{json.dumps(memory.get("repeat_rejects", []), indent=2)}

For EACH recommendation state the decision_basis field as either:
  - "DYNAMIC_BAND: [regime] vs peers" — when using distribution
  - "HARD_TARGET: [ratio]x target" — when using fixed target
  - Both when available

Generate 4-8 recommendations. Prioritise: (1) query control, (2) budget reallocation, (3) bids, (4) creative.
Include at least one DO_NOTHING if any campaigns have weak/unstable signals."""

    try:
        raw = _call(ENGINE_SYSTEM, user, ENGINE_MODEL, max_tokens=3000)
        return _parse_json(raw)
    except Exception as e:
        print(f"⚠ Rec engine API error: {e} — using fallback.")
        return _rec_engine_fallback(analyst_output, signals, target_cpa, query_summary)


def _rec_engine_fallback(analyst_output, signals, target_cpa, query_summary) -> list[dict]:
    """Deterministic fallback recommendations."""
    recs = []
    scalable    = [s for s in analyst_output if s.get("classification") == "SCALABLE"]
    diminishing = [s for s in analyst_output if s.get("classification") == "DIMINISHING"]

    if scalable and diminishing:
        from_camp = diminishing[0]["name"]
        to_camp   = scalable[0]["name"]
        sig_from  = next((s for s in signals if s["name"] == from_camp), {})
        recs.append({
            "type": "budget_reallocation",
            "campaign": from_camp,
            "segment": "generic",
            "intent_cluster": "unclassified",
            "cause": {"metric": "CPA", "diagnosed_cause": "DIMINISHING_RETURNS",
                      "evidence": scalable[0].get("key_insight", "")},
            "action": f"Reallocate Rs 10,000/day from {from_camp} to {to_camp}",
            "why": scalable[0].get("key_insight", "Scalable campaign with IS headroom"),
            "kpi_delta_estimate": "CPA -5% to -10% on account",
            "est_conv_delta": 15,
            "lever": "budget_reallocation",
            "priority_rank": 1,
            "change_cost": 1,
            "confidence": "Medium",
            "incrementality_flag": "GROWTH"
        })

    waste = query_summary.get("waste_patterns", [])
    if waste:
        recs.append({
            "type": "search_term_mining",
            "campaign": "Account-wide",
            "segment": "generic",
            "intent_cluster": "unclassified",
            "cause": {"metric": "Cost", "diagnosed_cause": "ZERO_CONV_SPEND",
                      "evidence": f"Rs {waste[0]['cost']:,.0f} spent on '{waste[0]['ngram']}' with 0 conversions"},
            "action": f"Add top {min(len(waste), 10)} waste n-grams as negatives: {', '.join(w['ngram'] for w in waste[:5])}",
            "why": f"Rs {sum(w['cost'] for w in waste[:10]):,.0f} total waste on zero-conv patterns",
            "kpi_delta_estimate": f"Save Rs {sum(w['cost'] for w in waste[:10]):,.0f}/week",
            "est_conv_delta": 0,
            "lever": "search_term_mining",
            "priority_rank": 2,
            "change_cost": 1,
            "confidence": "High",
            "incrementality_flag": "GROWTH"
        })
    return recs


# ── AGENT 3: GATEKEEPER ───────────────────────────────────────────────────────

GATEKEEPER_SYSTEM = """You are a Senior Strategic Gatekeeper for a Google Ads recommendation system.
Your job is to REJECT bad recommendations. Be strict.

For each recommendation, run ALL 12 checks:
1. Data sufficiency: >= 30 conv AND >= 300 clicks in 7d
2. Logical soundness: direction matches signal (never scale DIMINISHING)
3. Anti-hallucination: every number cited exists in the input data
4. Specificity: action contains specific Rs or % amount AND entity name
5. Stability: not based on < 3 day spike; volatility acknowledged
6. Bias detection: not over-indexed on brand (> 60% of batch)
7. Cause grounding: diagnosed cause exists and has HIGH/MEDIUM confidence
8. Incrementality: rec drives growth, not just brand harvesting
9. Position confound: copy recs use position-adjusted CTR, not raw
10. Portfolio net: account-level impact is non-negative
11. Change budget: campaign has remaining change budget today
12. DO_NOTHING quality: if DO_NOTHING, reason is specific and monitor list present

Output: JSON array only. Each object:
{
  "index": 0,
  "verdict": "APPROVED|REJECTED",
  "reason": null or specific rejection reason string,
  "checks_failed": []
}

Be especially strict about: vague actions ("optimise bids"), unquantified changes, scaling diminishing campaigns."""


def run_gatekeeper(recommendations: list[dict],
                   signals: list[dict],
                   target_cpa: float,
                   change_budgets: dict) -> list[dict]:
    """Agent 3: Validate all recommendations. Returns verdict per rec."""
    sufficient_count = sum(1 for s in signals if s["confidence"] == "SUFFICIENT")
    brand_count      = sum(1 for r in recommendations if r.get("segment") == "brand")
    brand_pct        = brand_count / len(recommendations) if recommendations else 0

    user = f"""Context:
- Target CPA: Rs {target_cpa}
- Campaigns with sufficient data: {sufficient_count}
- Brand recommendation fraction: {brand_pct:.0%} (flag if > 60%)
- Change budgets: {json.dumps({k: v.get('remaining', 2) for k, v in change_budgets.items()}, indent=2)}

Recommendations to validate:
{json.dumps([{"index": i, **r} for i, r in enumerate(recommendations)], indent=2)}

Run all 12 checks on each recommendation. Return the verdict array."""

    try:
        raw = _call(GATEKEEPER_SYSTEM, user, GATEKEEPER_MODEL, max_tokens=2000)
        return _parse_json(raw)
    except Exception as e:
        print(f"⚠ Gatekeeper API error: {e} — using local checks.")
        return _gatekeeper_fallback(recommendations, change_budgets)


def _gatekeeper_fallback(recs: list[dict], change_budgets: dict) -> list[dict]:
    """Deterministic fallback validation when API unavailable."""
    results = []
    for i, r in enumerate(recs):
        failed = []
        action = str(r.get("action", ""))
        why    = str(r.get("why", ""))

        # Check specificity
        import re
        has_amount = bool(re.search(r'Rs\s*[\d,]+|[\d.]+%|\d+\s*conversions', action + why, re.I))
        if not has_amount:
            failed.append("specificity: no Rs or % amount in action")

        # Check vague words
        vague = ["optimise", "optimize", "improve", "consider", "better", "enhance"]
        if any(v in action.lower() for v in vague):
            failed.append("specificity: vague language detected")

        # Check scaling diminishing
        if r.get("type") == "budget_reallocation":
            camp_signal = next((s for s in [] if s.get("name") == r.get("campaign")), {})
            if camp_signal.get("classification") == "DIMINISHING":
                failed.append("logical_soundness: scaling DIMINISHING campaign")

        # Check change budget
        budget = change_budgets.get(r.get("campaign", ""), {}).get("remaining", 2)
        if budget <= 0 and r.get("type") != "DO_NOTHING":
            failed.append("change_budget: no remaining budget for today")

        verdict = "APPROVED" if not failed else "REJECTED"
        results.append({
            "index": i,
            "verdict": verdict,
            "reason": "; ".join(failed) if failed else None,
            "checks_failed": failed
        })
    return results
