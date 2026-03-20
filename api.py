"""
api.py
Flask API server — all 11 endpoints.
Entry point for Railway (Gunicorn runs this).
"""
import os
import json
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)  # Allow frontend to call from any origin


# ── STARTUP ───────────────────────────────────────────────────────────────────

@app.before_request
def startup():
    """Ensure tables exist on first request."""
    if not getattr(app, '_tables_created', False):
        try:
            from memory import setup_tables
            setup_tables()
            app._tables_created = True
        except Exception as e:
            print(f"⚠ DB setup failed: {e}")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def ok(data):
    return jsonify({"status": "ok", "data": data})


def err(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


def _serialize(obj):
    """Make objects JSON-serialisable."""
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    if hasattr(obj, '__dict__'):
        return obj.__dict__
    return str(obj)


# ── ENDPOINT 1: HEALTH ────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check. Verifies DB connection."""
    try:
        from memory import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"

    return ok({
        "status":    "ok",
        "db":        db_status,
        "timestamp": datetime.now().isoformat(),
        "version":   "3.0"
    })


# ── ENDPOINT 2: RUN PIPELINE ──────────────────────────────────────────────────

@app.route("/run", methods=["GET", "POST"])
def run_pipeline():
    """Trigger full pipeline. Body: { customer_id, target_cpa, dry_run, scope }
    GET request always runs as dry_run=True for browser testing."""
    body        = request.get_json(silent=True) or {}
    # GET from browser = always dry run
    if request.method == "GET":
        body["dry_run"] = True
    customer_id = body.get("customer_id") or os.getenv("CUSTOMER_ID", "demo")
    target_cpa  = float(body.get("target_cpa") or os.getenv("DEFAULT_TARGET_CPA", 250))
    dry_run     = bool(body.get("dry_run", False))
    scope       = body.get("scope")  # list of campaign IDs or None

    try:
        from pipeline import run
        results = run(
            customer_id=customer_id,
            target_cpa=target_cpa,
            dry_run=dry_run,
            scope=scope
        )
        return ok({
            "approved":      len(results["approved"]),
            "rejected":      len(results["rejected"]),
            "do_nothing":    len(results["do_nothing"]),
            "recommendations": json.loads(
                json.dumps(results["approved"], default=_serialize)
            ),
            "stats":         results["stats"],
            "duration":      results["duration"],
        })
    except Exception as e:
        traceback.print_exc()
        return err(str(e), 500)


# ── ENDPOINT 3: GET RECOMMENDATIONS ──────────────────────────────────────────

@app.route("/recommendations", methods=["GET"])
def get_recommendations():
    """Fetch latest recommendations. Query params: status (approved|rejected|all), limit."""
    status = request.args.get("status", "approved")
    limit  = int(request.args.get("limit", 50))

    try:
        from memory import get_recent_recommendations
        recs = get_recent_recommendations(limit=limit, status=status if status != "all" else None)
        return ok(json.loads(json.dumps(recs, default=_serialize)))
    except Exception as e:
        return err(str(e), 500)


# ── ENDPOINT 4: HUMAN FEEDBACK ────────────────────────────────────────────────

@app.route("/feedback", methods=["POST"])
def record_feedback():
    """Record human accept/reject/defer. Body: { rec_id, feedback }"""
    body     = request.get_json() or {}
    rec_id   = body.get("rec_id")
    feedback = body.get("feedback")

    if not rec_id or feedback not in ("accepted", "rejected", "deferred"):
        return err("rec_id and feedback (accepted|rejected|deferred) required")

    try:
        from memory import record_feedback
        record_feedback(int(rec_id), feedback)
        return ok({"rec_id": rec_id, "feedback": feedback})
    except Exception as e:
        return err(str(e), 500)


# ── ENDPOINT 5: CONFIRM IMPLEMENTATION ───────────────────────────────────────

@app.route("/implement", methods=["POST"])
def confirm_implementation():
    """
    Mark a recommendation as implemented. Locks T0 KPI snapshot.
    Body: { rec_id, notes }
    """
    body   = request.get_json() or {}
    rec_id = body.get("rec_id")

    if not rec_id:
        return err("rec_id required")

    try:
        from memory import get_recent_recommendations, confirm_implementation
        # Find the rec to build T0 snapshot
        recs = get_recent_recommendations(limit=200)
        rec  = next((r for r in recs if r["id"] == int(rec_id)), None)
        if not rec:
            return err(f"Recommendation {rec_id} not found")

        payload = rec.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)

        t0_snapshot = {
            "primary_metric": rec.get("cpa_before"),
            "kpi":            payload.get("kpi", "CPA"),
            "diagnosed_cause": payload.get("cause", {}).get("diagnosed_cause", ""),
            "campaign":       rec.get("campaign"),
            "rec_type":       rec.get("rec_type"),
            "timestamp":      datetime.now().isoformat(),
        }

        impl_id = confirm_implementation(int(rec_id), t0_snapshot)
        from memory import record_feedback
        record_feedback(int(rec_id), "accepted")

        return ok({
            "rec_id":  rec_id,
            "impl_id": impl_id,
            "t0_locked_at": datetime.now().isoformat(),
            "outcome_due":  "Check back in 7 days via GET /outcomes"
        })
    except Exception as e:
        traceback.print_exc()
        return err(str(e), 500)


# ── ENDPOINT 6: MEMORY / HISTORY ─────────────────────────────────────────────

@app.route("/memory", methods=["GET"])
def get_memory():
    """Fetch memory context: accuracy scores, recent approved, repeat rejects."""
    try:
        from memory import get_memory_context, get_run_history
        ctx     = get_memory_context(lookback=20)
        history = get_run_history(limit=30)
        return ok({
            "accuracy_scores":  ctx["accuracy_scores"],
            "repeat_rejects":   ctx["repeat_rejects"],
            "recent_approved":  ctx["recent_approved"][:10],
            "run_history":      json.loads(json.dumps(history, default=_serialize))
        })
    except Exception as e:
        return err(str(e), 500)


# ── ENDPOINT 7: GET CAMPAIGN CONFIG ──────────────────────────────────────────

@app.route("/config/<campaign_id>", methods=["GET"])
def get_config(campaign_id):
    from memory import get_campaign_config
    cfg = get_campaign_config(campaign_id)
    if not cfg:
        return err(f"No config found for {campaign_id}", 404)
    return ok(json.loads(json.dumps(cfg, default=_serialize)))


# ── ENDPOINT 8: UPDATE CAMPAIGN CONFIG ───────────────────────────────────────

@app.route("/config/<campaign_id>", methods=["PUT"])
def update_config(campaign_id):
    """
    Upsert campaign config. Body: { kpi, target_value, lever_weights, ... }
    This is the main way to change KPI targets and lever priorities.
    """
    body = request.get_json() or {}
    body["campaign_id"] = campaign_id

    try:
        from memory import upsert_campaign_config
        upsert_campaign_config(body)
        return ok({"campaign_id": campaign_id, "updated": True})
    except Exception as e:
        return err(str(e), 500)


# ── ENDPOINT 9: PORTFOLIO STATE ───────────────────────────────────────────────

@app.route("/portfolio", methods=["GET"])
def get_portfolio():
    """Current portfolio state: category caps and spend."""
    try:
        from memory import get_conn
        from data_pull import pull_campaign_data
        from normalizer import normalize

        caps = {}
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM portfolio_config")
                caps = {r["category"]: r["daily_budget_cap"] for r in cur.fetchall()}

        customer_id = os.getenv("CUSTOMER_ID", "demo")
        df      = pull_campaign_data(customer_id, days=7)
        signals = normalize(df)

        from collections import defaultdict
        by_product = defaultdict(float)
        for s in signals:
            by_product[s.get("product", "unknown")] += s.get("cost_7d", 0) / 7

        return ok({
            "category_caps":       caps,
            "current_daily_spend": {k: round(v, 2) for k, v in by_product.items()},
            "headroom": {
                cat: round(caps[cat] - by_product.get(cat, 0), 2)
                for cat in caps
            }
        })
    except Exception as e:
        return err(str(e), 500)


# ── ENDPOINT 10: OUTCOMES ─────────────────────────────────────────────────────

@app.route("/outcomes", methods=["GET"])
def get_outcomes():
    """Fetch 7-day implementation outcomes."""
    try:
        from memory import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT i.*, r.campaign, r.rec_type, r.lever, r.action
                    FROM implementations i
                    JOIN recommendations r ON i.rec_id = r.id
                    WHERE i.t7_snapshot IS NOT NULL
                    ORDER BY i.confirmed_at DESC LIMIT 50
                """)
                outcomes = [dict(row) for row in cur.fetchall()]
        return ok(json.loads(json.dumps(outcomes, default=_serialize)))
    except Exception as e:
        return err(str(e), 500)


# ── ENDPOINT 11: DO-NOTHING LOG ───────────────────────────────────────────────

@app.route("/do-nothing", methods=["GET"])
def get_do_nothing():
    """Fetch all DO_NOTHING outputs from the most recent run."""
    try:
        from memory import get_recent_recommendations
        all_recs   = get_recent_recommendations(limit=100)
        do_nothing = []
        for r in all_recs:
            p = r.get("payload") or {}
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except Exception:
                    p = {}
            if p.get("type") == "DO_NOTHING" or r.get("rec_type") == "DO_NOTHING":
                do_nothing.append({
                    "id":       r["id"],
                    "campaign": r.get("campaign"),
                    "reason":   p.get("reason", r.get("reject_reason")),
                    "monitor":  p.get("monitor", []),
                    "run_date": r.get("run_date"),
                })
        return ok(json.loads(json.dumps(do_nothing, default=_serialize)))
    except Exception as e:
        return err(str(e), 500)


# ── SEED ENDPOINT (development only) ─────────────────────────────────────────

@app.route("/seed", methods=["GET", "POST"])
def seed_configs():
    """
    Seed sample campaign configs for Digital Gold account.
    Only use during initial setup — safe to run multiple times (upsert).
    POST /seed
    """
    from memory import upsert_campaign_config

    sample_configs = [
        {
            "campaign_id": "Search_Digital_Gold_Brand",
            "product": "digital_gold", "kpi": "CPA", "target_value": 180,
            "active": True, "scope": "include", "max_actions_per_day": 1,
            "lever_weights": {"budget_reallocation": 4, "bid_adjustment": 6,
                              "ad_copy": 8, "search_term_mining": 5},
            "business_context": {"phase": "maintain", "budget_freeze": False}
        },
        {
            "campaign_id": "Search_Digital_Gold_Generic",
            "product": "digital_gold", "kpi": "CPA", "target_value": 250,
            "active": True, "scope": "include", "max_actions_per_day": 2,
            "lever_weights": {"budget_reallocation": 9, "bid_adjustment": 7,
                              "ad_copy": 9, "search_term_mining": 8},
            "business_context": {"phase": "scale", "budget_freeze": False}
        },
        {
            "campaign_id": "Search_Insurance_Intent",
            "product": "insurance", "kpi": "CPL", "target_value": 400,
            "active": True, "scope": "include", "max_actions_per_day": 2,
            "lever_weights": {"budget_reallocation": 8, "ad_copy": 9,
                              "search_term_mining": 7, "match_type": 6},
            "business_context": {"phase": "scale", "budget_freeze": False}
        },
        {
            "campaign_id": "Search_MutualFund_Exact",
            "product": "mutual_fund", "kpi": "CPA", "target_value": 1200,
            "active": True, "scope": "include", "max_actions_per_day": 1,
            "lever_weights": {"bid_adjustment": 7, "ad_copy": 8,
                              "search_term_mining": 6},
            "business_context": {"phase": "maintain", "budget_freeze": False}
        },
        {
            "campaign_id": "Search_Loans_Generic",
            "product": "loans", "kpi": "CPA", "target_value": 350,
            "active": True, "scope": "include", "max_actions_per_day": 2,
            "lever_weights": {"budget_reallocation": 8, "search_term_mining": 9,
                              "ad_copy": 7},
            "business_context": {"phase": "scale", "budget_freeze": False}
        },
    ]

    for cfg in sample_configs:
        upsert_campaign_config(cfg)

    return ok({"seeded": len(sample_configs), "campaigns": [c["campaign_id"] for c in sample_configs]})


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
