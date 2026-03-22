"""
data_pull.py
Fetches campaign + search term data from Google Ads API.
Falls back to sample data if credentials are not configured.
"""
import os
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()


def _ads_available() -> bool:
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "CUSTOMER_ID"
    ]
    return all(os.getenv(k) for k in required)


def pull_campaign_data(customer_id: str, days: int = 14) -> pd.DataFrame:
    """
    Pull campaign-level metrics for the last `days` days.
    Returns a DataFrame with both 7d and 14d window aggregates.
    Falls back to sample data if Google Ads API is not configured.
    """
    if not _ads_available():
        print("⚠ Google Ads credentials not found — using sample data.")
        return _sample_campaign_data()

    try:
        from google.ads.googleads.client import GoogleAdsClient
        return _pull_from_api(customer_id, days)
    except Exception as e:
        print(f"⚠ Google Ads API error: {e} — using sample data.")
        return _sample_campaign_data()


def pull_search_term_data(customer_id: str, days: int = 14) -> pd.DataFrame:
    """
    Pull search term view data. Returns query-level metrics.
    Falls back to sample data if credentials not configured.
    """
    if not _ads_available():
        return _sample_search_term_data()
    try:
        from google.ads.googleads.client import GoogleAdsClient
        return _pull_search_terms_from_api(customer_id, days)
    except Exception as e:
        print(f"⚠ Google Ads search term API error: {e} — using sample data.")
        return _sample_search_term_data()


# ── REAL API PULL ─────────────────────────────────────────────────────────────

def _build_client():
    from google.ads.googleads.client import GoogleAdsClient
    config = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""),
        "use_proto_plus": True,
    }
    return GoogleAdsClient.load_from_dict(config)


def _pull_from_api(customer_id: str, days: int) -> pd.DataFrame:
    client = _build_client()
    ga_service = client.get_service("GoogleAdsService")

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          metrics.cost_micros,
          metrics.clicks,
          metrics.impressions,
          metrics.conversions,
          metrics.conversions_value,
          metrics.search_impression_share,
          metrics.search_budget_lost_impression_share,
          metrics.search_rank_lost_impression_share,
          metrics.search_absolute_top_impression_share,
          segments.date
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND search_term_view.status = 'NONE'
        ORDER BY metrics.cost_micros DESC
        LIMIT 500
```

The change is:
- `!= 'EXCLUDED'` → `= 'NONE'` — only unactioned search terms
- Added `LIMIT 500` — top 500 by spend only

Commit directly to main. Then also commit the `Procfile` timeout fix if you haven't already:
```
web: gunicorn api:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
    """

    rows = []
    response = ga_service.search(customer_id=str(customer_id), query=query)
    for row in response:
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "date": row.segments.date,
            "cost": row.metrics.cost_micros / 1_000_000,
            "clicks": row.metrics.clicks,
            "impressions": row.metrics.impressions,
            "conversions": row.metrics.conversions,
            "conversion_value": row.metrics.conversions_value,
            "is_captured": float(row.metrics.search_impression_share or 0),
            "is_lost_budget": float(row.metrics.search_budget_lost_impression_share or 0),
            "is_lost_rank": float(row.metrics.search_rank_lost_impression_share or 0),
            "is_abs_top": float(row.metrics.search_absolute_top_impression_share or 0),
        })

    if not rows:
        print("⚠ No rows returned from Google Ads API — using sample data.")
        return _sample_campaign_data()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return _aggregate_windows(df)


def _pull_search_terms_from_api(customer_id: str, days: int) -> pd.DataFrame:
    client = _build_client()
    ga_service = client.get_service("GoogleAdsService")

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    query = f"""
        SELECT
          search_term_view.search_term,
          search_term_view.status,
          campaign.name,
          ad_group.name,
          metrics.cost_micros,
          metrics.clicks,
          metrics.impressions,
          metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND search_term_view.status != 'EXCLUDED'
        ORDER BY metrics.cost_micros DESC
    """

    rows = []
    response = ga_service.search(customer_id=str(customer_id), query=query)
    for row in response:
        rows.append({
            "query": row.search_term_view.search_term,
            "campaign_name": row.campaign.name,
            "ad_group": row.ad_group.name,
            "cost": row.metrics.cost_micros / 1_000_000,
            "clicks": row.metrics.clicks,
            "impressions": row.metrics.impressions,
            "conversions": row.metrics.conversions,
        })

    return pd.DataFrame(rows) if rows else _sample_search_term_data()


# ── AGGREGATION ───────────────────────────────────────────────────────────────

def _aggregate_windows(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a raw daily DataFrame into 7d and 14d campaign windows."""
    now = pd.Timestamp.today().normalize()

    def agg(d):
        return d.groupby(["campaign_id", "campaign_name"]).agg(
            cost=("cost", "sum"),
            clicks=("clicks", "sum"),
            impressions=("impressions", "sum"),
            conversions=("conversions", "sum"),
            conversion_value=("conversion_value", "sum"),
            is_lost_budget=("is_lost_budget", "mean"),
            is_lost_rank=("is_lost_rank", "mean"),
            is_abs_top=("is_abs_top", "mean"),
            is_captured=("is_captured", "mean"),
        ).reset_index()

    df7  = df[df["date"] >= now - pd.Timedelta(days=7)]
    df14 = df[df["date"] >= now - pd.Timedelta(days=14)]

    w7  = agg(df7).add_suffix("_7d")
    w7  = w7.rename(columns={"campaign_id_7d": "campaign_id", "campaign_name_7d": "campaign_name"})

    w14 = agg(df14)[["campaign_id", "cost", "conversions", "conversion_value"]]
    w14 = w14.add_suffix("_14d").rename(columns={"campaign_id_14d": "campaign_id"})

    merged = w7.merge(w14, on="campaign_id", how="left")
    merged["conversion_value_14d"] = merged.get("conversion_value_14d", 0).fillna(0)
    merged["conversions_14d"]       = merged.get("conversions_14d", 0).fillna(0)
    merged["cost_14d"]              = merged.get("cost_14d", 0).fillna(0)
    return merged


# ── SAMPLE DATA ───────────────────────────────────────────────────────────────

def _sample_campaign_data() -> pd.DataFrame:
    """Realistic sample data matching Paytm Digital Gold account structure."""
    data = {
        "campaign_id":       ["C001","C002","C003","C004","C005","C006","C007","C008"],
        "campaign_name":     [
            "Search_Digital_Gold_Brand",
            "Search_Digital_Gold_Generic",
            "Search_Insurance_Intent",
            "Search_MutualFund_Exact",
            "Search_CreditCard_Broad",
            "Search_Loans_Generic",
            "Search_Recharge_Brand",
            "Search_Travel_Insurance",
        ],
        "cost_7d":           [185000, 92000, 67000, 54000, 78000, 43000, 28000, 31000],
        "clicks_7d":         [4200,   1850,   980,   760,  1420,   620,  3100,   440],
        "impressions_7d":    [38000, 52000, 18000, 12000, 35000, 9800, 41000,  8200],
        "conversions_7d":    [820,    180,   142,    38,    22,    95,   610,    12],
        "conversion_value_7d":[2050000,450000,497000,114000,66000,285000,1525000,36000],
        "is_lost_budget_7d": [0.08,  0.34,  0.41,  0.12, 0.05, 0.29, 0.03, 0.18],
        "is_lost_rank_7d":   [0.04,  0.18,  0.22,  0.28, 0.09, 0.15, 0.02, 0.31],
        "is_abs_top_7d":     [0.72,  0.21,  0.18,  0.44, 0.31, 0.29, 0.88, 0.14],
        "is_captured_7d":    [0.88,  0.48,  0.37,  0.60, 0.86, 0.56, 0.95, 0.51],
        "cost_14d":          [362000,178000,131000,102000,148000,84000,55000,58000],
        "conversions_14d":   [1580,   330,   268,    58,    34,   176, 1190,   19],
        "conversion_value_14d":[3950000,825000,938000,174000,102000,528000,2975000,57000],
    }
    df = pd.DataFrame(data)
    # Derive missing columns with defaults
    for col in ["is_lost_budget_7d","is_lost_rank_7d","is_abs_top_7d","is_captured_7d"]:
        if col not in df.columns:
            df[col] = 0.1
    return df


def _sample_search_term_data() -> pd.DataFrame:
    data = {
        "query": [
            "buy digital gold online", "digital gold investment app",
            "paytm gold", "best digital gold platform",
            "virtual gold investment", "digital gold scheme",
            "invest in gold online", "cheap gold investment",
            "online gold purchase", "digital gold vs physical gold",
            "paytm gold buy", "gold investment india",
            "term insurance online", "best term plan",
            "life insurance compare", "cheap term insurance",
        ],
        "campaign_name": [
            "Search_Digital_Gold_Generic","Search_Digital_Gold_Generic",
            "Search_Digital_Gold_Brand","Search_Digital_Gold_Generic",
            "Search_Digital_Gold_Generic","Search_Digital_Gold_Generic",
            "Search_Digital_Gold_Generic","Search_Digital_Gold_Generic",
            "Search_Digital_Gold_Generic","Search_Digital_Gold_Generic",
            "Search_Digital_Gold_Brand","Search_Digital_Gold_Generic",
            "Search_Insurance_Intent","Search_Insurance_Intent",
            "Search_Insurance_Intent","Search_Insurance_Intent",
        ],
        "ad_group": [
            "Buy Digital Gold","Buy Digital Gold",
            "Brand Core","Buy Digital Gold",
            "Low Intent","Low Intent",
            "Buy Digital Gold","Low Intent",
            "Buy Digital Gold","Digital Gold Info",
            "Brand Core","Digital Gold Info",
            "Term Insurance","Term Insurance",
            "Compare Insurance","Term Insurance",
        ],
        "cost":        [4200,3800,2100,1900,1600,1400,1200,980,870,750,650,520,3100,2800,2400,1900],
        "clicks":      [180, 162,  95,  82,  70,  62,  54,  42, 38, 32, 28, 22, 140, 125, 108,  85],
        "impressions": [2100,1900,600,1200,1400,1300,900,1100,800,950,350,700,1600,1450,1350,1100],
        "conversions": [42,  38,  28,  14,   6,   4,  12,   2,  8,  0, 18,  0,  35,  28,  22,  14],
    }
    return pd.DataFrame(data)


if __name__ == "__main__":
    customer_id = os.getenv("CUSTOMER_ID", "demo")
    df = pull_campaign_data(customer_id)
    print(f"Pulled {len(df)} campaigns.")
    print(df[["campaign_name", "cost_7d", "conversions_7d"]].to_string())
