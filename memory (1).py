"""
memory.py
All PostgreSQL read/write operations.
Single source of truth for DB access across the entire system.
"""
import os
import json
import math
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()


def get_conn():
    """Return a database connection. Handles Railway's postgres:// prefix."""
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


# ── SETUP ─────────────────────────────────────────────────────────────────────

def setup_tables():
    """Create all tables if they don't exist. Safe to run on every startup."""
    sql = """
    CREATE TABLE IF NOT EXISTS campaign_configs (
        campaign_id          TEXT PRIMARY KEY,
        product              TEXT,
        kpi                  TEXT DEFAULT 'CPA',
        target_value         FLOAT DEFAULT 250,
        active               BOOLEAN DEFAULT true,
        scope                TEXT DEFAULT 'include',
        max_actions_per_day  INT DEFAULT 2,
        lever_weights        JSONB DEFAULT '{}',
        business_context     JSONB DEFAULT '{}',
        updated_at           TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS portfolio_config (
        category             TEXT PRIMARY KEY,
        daily_budget_cap     FLOAT DEFAULT 0,
        notes                TEXT,
        updated_at           TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS recommendations (
        id               SERIAL PRIMARY KEY,
        run_date         TIMESTAMP DEFAULT NOW(),
        campaign         TEXT,
        segment          TEXT,
        intent_cluster   TEXT,
        rec_type         TEXT,
        lever            TEXT,
        action           TEXT,
        cause_json       JSONB,
        confidence       TEXT,
        gatekeeper       TEXT,
        reject_reason    TEXT,
        human_feedback   TEXT,
        cpa_before       FLOAT,
        payload          JSONB
    );

    CREATE TABLE IF NOT EXISTS implementations (
        id               SERIAL PRIMARY KEY,
        rec_id           INT REFERENCES recommendations(id),
        confirmed_at     TIMESTAMP DEFAULT NOW(),
        t0_snapshot      JSONB,
        t7_snapshot      JSONB,
        cause_validated  BOOLEAN,
        kpi_delta        FLOAT,
        outcome          TEXT
    );

    CREATE TABLE IF NOT EXISTS accuracy_scores (
        lever            TEXT,
        campaign_class   TEXT,
        total_approved   INT DEFAULT 0,
        human_accepted   INT DEFAULT 0,
        cpa_improved     INT DEFAULT 0,
        accuracy_score   FLOAT DEFAULT 0.5,
        updated_at       TIMESTAMP DEFAULT NOW(),
        PRIMARY KEY (lever, campaign_class)
    );

    CREATE TABLE IF NOT EXISTS change_log (
        id           SERIAL PRIMARY KEY,
        campaign_id  TEXT,
        action_type  TEXT,
        acted_at     TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS run_log (
        id              SERIAL PRIMARY KEY,
        run_date        TIMESTAMP DEFAULT NOW(),
        campaigns_total INT,
        sufficient      INT,
        recs_generated  INT,
        recs_approved   INT,
        recs_rejected   INT,
        duration_secs   FLOAT
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print("✓ Database tables ready.")


# ── CAMPAIGN CONFIG ───────────────────────────────────────────────────────────

DEFAULT_LEVER_WEIGHTS = {
    "budget_reallocation": 8,
    "bid_adjustment": 6,
    "ad_copy": 9,
    "search_term_mining": 7,
    "match_type": 5,
    "landing_page": 4,
    "audience_layering": 3,
    "dayparting": 4,
    "device_reallocation": 5,
    "quality_score": 6
}

def get_campaign_config(campaign_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM campaign_configs WHERE campaign_id = %s", (campaign_id,))
            row = cur.fetchone()
    if not row:
        return None
    cfg = dict(row)
    # Merge default lever weights with any overrides stored in DB
    weights = DEFAULT_LEVER_WEIGHTS.copy()
    if cfg.get("lever_weights"):
        weights.update(cfg["lever_weights"])
    cfg["lever_weights"] = weights
    return cfg


def get_all_active_configs() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM campaign_configs WHERE active = true AND scope = 'include'"
            )
            rows = cur.fetchall()
    result = []
    for row in rows:
        cfg = dict(row)
        weights = DEFAULT_LEVER_WEIGHTS.copy()
        if cfg.get("lever_weights"):
            weights.update(cfg["lever_weights"])
        cfg["lever_weights"] = weights
        result.append(cfg)
    return result


def upsert_campaign_config(cfg: dict):
    sql = """
    INSERT INTO campaign_configs
        (campaign_id, product, kpi, target_value, active, scope,
         max_actions_per_day, lever_weights, business_context, updated_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
    ON CONFLICT (campaign_id) DO UPDATE SET
        product             = EXCLUDED.product,
        kpi                 = EXCLUDED.kpi,
        target_value        = EXCLUDED.target_value,
        active              = EXCLUDED.active,
        scope               = EXCLUDED.scope,
        max_actions_per_day = EXCLUDED.max_actions_per_day,
        lever_weights       = EXCLUDED.lever_weights,
        business_context    = EXCLUDED.business_context,
        updated_at          = NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                cfg["campaign_id"], cfg.get("product", "unknown"),
                cfg.get("kpi", "CPA"), cfg.get("target_value", 250),
                cfg.get("active", True), cfg.get("scope", "include"),
                cfg.get("max_actions_per_day", 2),
                json.dumps(cfg.get("lever_weights", {})),
                json.dumps(cfg.get("business_context", {}))
            ))
        conn.commit()


# ── RECOMMENDATIONS ───────────────────────────────────────────────────────────

def _sanitize(obj):
    """Recursively replace non-JSON-safe values (Infinity, NaN) with None."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def save_recommendations(recs: list[dict]) -> list[int]:
    """Save recommendations and return their DB IDs."""
    ids = []
    sql = """
    INSERT INTO recommendations
        (campaign, segment, intent_cluster, rec_type, lever, action,
         cause_json, confidence, gatekeeper, reject_reason, cpa_before, payload)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    RETURNING id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            for r in recs:
                safe_r    = _sanitize(r)
                safe_cause= _sanitize(r.get("cause", {}))
                cur.execute(sql, (
                    r.get("campaign"), r.get("segment"), r.get("intent_cluster"),
                    r.get("type"), r.get("lever"), r.get("action"),
                    json.dumps(safe_cause), r.get("confidence"),
                    r.get("verdict", "PENDING"), r.get("rejection_reason"),
                    r.get("cpa_before"), json.dumps(safe_r)
                ))
                ids.append(cur.fetchone()["id"])
        conn.commit()
    return ids


def get_recent_recommendations(limit: int = 50, status: str = None,
                                 today_only: bool = True) -> list[dict]:
    """
    Return recommendations. By default returns only TODAY's latest run
    to avoid showing duplicate recs from multiple daily runs.
    Set today_only=False to get full history.
    """
    conditions = []
    params = []

    if today_only:
        # Only show recs from the most recent run (by run_date::date = today OR latest run_date)
        conditions.append("""id IN (
            SELECT id FROM recommendations
            WHERE run_date::date = (SELECT MAX(run_date::date) FROM recommendations)
        )""")

    if status == "approved":
        conditions.append("gatekeeper = 'APPROVED'")
    elif status == "rejected":
        conditions.append("gatekeeper = 'REJECTED'")
    elif status == "DO_NOTHING":
        conditions.append("rec_type = 'DO_NOTHING'")

    sql = "SELECT * FROM recommendations"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY run_date DESC, id DESC LIMIT %s"
    params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def record_feedback(rec_id: int, feedback: str):
    """Record human accept/reject/defer on a recommendation."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE recommendations SET human_feedback = %s WHERE id = %s",
                (feedback, rec_id)
            )
        conn.commit()


# ── IMPLEMENTATIONS + OUTCOME ─────────────────────────────────────────────────

def confirm_implementation(rec_id: int, t0_snapshot: dict) -> int:
    """Lock T0 snapshot when marketer confirms implementation. Returns impl ID."""
    sql = """
    INSERT INTO implementations (rec_id, t0_snapshot)
    VALUES (%s, %s) RETURNING id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (rec_id, json.dumps(t0_snapshot)))
            impl_id = cur.fetchone()["id"]
        conn.commit()
    return impl_id


def get_pending_outcomes() -> list[dict]:
    """Find implementations confirmed > 7 days ago without a T7 snapshot."""
    cutoff = datetime.now() - timedelta(days=7)
    sql = """
    SELECT i.*, r.campaign, r.rec_type, r.lever, r.payload
    FROM implementations i
    JOIN recommendations r ON i.rec_id = r.id
    WHERE i.confirmed_at < %s AND i.t7_snapshot IS NULL
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cutoff,))
            return [dict(r) for r in cur.fetchall()]


def save_outcome(impl_id: int, t7_snapshot: dict, kpi_delta: float,
                 outcome: str, cause_validated: bool):
    sql = """
    UPDATE implementations
    SET t7_snapshot = %s, kpi_delta = %s, outcome = %s, cause_validated = %s
    WHERE id = %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                json.dumps(t7_snapshot), kpi_delta, outcome, cause_validated, impl_id
            ))
        conn.commit()


# ── ACCURACY + MEMORY ─────────────────────────────────────────────────────────

def get_accuracy_scores() -> dict:
    """Return accuracy scores keyed by lever."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lever, accuracy_score FROM accuracy_scores")
            rows = cur.fetchall()
    return {r["lever"]: r["accuracy_score"] for r in rows}


def recompute_accuracy_scores():
    """Recalculate accuracy per lever from outcome history. Run weekly."""
    sql = """
    INSERT INTO accuracy_scores
        (lever, campaign_class, total_approved, human_accepted, cpa_improved,
         accuracy_score, updated_at)
    SELECT
        r.lever,
        r.payload->>'classification' AS campaign_class,
        COUNT(*) AS total_approved,
        SUM(CASE WHEN r.human_feedback = 'accepted' THEN 1 ELSE 0 END) AS human_accepted,
        SUM(CASE WHEN i.outcome = 'POSITIVE' THEN 1 ELSE 0 END) AS cpa_improved,
        ROUND(
            COALESCE(
                SUM(CASE WHEN r.human_feedback = 'accepted' THEN 1.0 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 0.5
            )::numeric, 3
        ) AS accuracy_score,
        NOW()
    FROM recommendations r
    LEFT JOIN implementations i ON i.rec_id = r.id
    WHERE r.gatekeeper = 'APPROVED'
    GROUP BY r.lever, r.payload->>'classification'
    ON CONFLICT (lever, campaign_class) DO UPDATE SET
        total_approved = EXCLUDED.total_approved,
        human_accepted = EXCLUDED.human_accepted,
        cpa_improved   = EXCLUDED.cpa_improved,
        accuracy_score = EXCLUDED.accuracy_score,
        updated_at     = NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def get_memory_context(lookback: int = 10) -> dict:
    """Full memory context injected into Claude prompts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.campaign, r.rec_type, r.lever, r.action,
                       r.human_feedback, i.outcome, i.kpi_delta
                FROM recommendations r
                LEFT JOIN implementations i ON i.rec_id = r.id
                WHERE r.gatekeeper = 'APPROVED'
                ORDER BY r.run_date DESC LIMIT %s
            """, (lookback,))
            recent = [dict(row) for row in cur.fetchall()]

            cur.execute("""
                SELECT campaign, rec_type, COUNT(*) AS times
                FROM recommendations
                WHERE human_feedback = 'rejected'
                GROUP BY campaign, rec_type
                HAVING COUNT(*) >= 2
            """)
            repeat_rejects = [dict(row) for row in cur.fetchall()]

    return {
        "recent_approved": recent,
        "accuracy_scores": get_accuracy_scores(),
        "repeat_rejects": repeat_rejects
    }


# ── CHANGE CONTROL ────────────────────────────────────────────────────────────

def check_change_budget(campaign_id: str, max_actions: int) -> dict:
    """Check if a campaign has remaining change budget for today."""
    today = datetime.now().date()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS count FROM change_log
                WHERE campaign_id = %s AND acted_at::date = %s
            """, (campaign_id, today))
            used = cur.fetchone()["count"]
    remaining = max_actions - used
    return {"used": used, "remaining": remaining, "can_act": remaining > 0}


def log_action(campaign_id: str, action_type: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO change_log (campaign_id, action_type) VALUES (%s, %s)",
                (campaign_id, action_type)
            )
        conn.commit()


# ── RUN LOG ───────────────────────────────────────────────────────────────────

def log_run(stats: dict):
    sql = """
    INSERT INTO run_log
        (campaigns_total, sufficient, recs_generated, recs_approved, recs_rejected, duration_secs)
    VALUES (%s,%s,%s,%s,%s,%s)
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                stats.get("campaigns_total", 0),
                stats.get("sufficient", 0),
                stats.get("recs_generated", 0),
                stats.get("recs_approved", 0),
                stats.get("recs_rejected", 0),
                stats.get("duration_secs", 0)
            ))
        conn.commit()


def get_run_history(limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM run_log ORDER BY run_date DESC LIMIT %s", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]


if __name__ == "__main__":
    setup_tables()
    print("Database initialised successfully.")
