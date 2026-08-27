# HRF Track 02 — Point-in-Time Development Universe Engine v0.1
# Outcome-blind. Does not read H15 / MFE / MAE / tail outcomes.

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable
import re
import numpy as np
import pandas as pd


PREFERRED_NAME_RE = re.compile(
    r"(?:우|우B|우C|우선주|\d+우|\d+우B|\d+우C)$",
    re.IGNORECASE,
)
SPAC_NAME_RE = re.compile(r"(?:스팩|SPAC)", re.IGNORECASE)


@dataclass(frozen=True)
class UniverseConfig:
    markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    liquidity_lookback_sessions: int = 20
    business_day_calendar_days: int = 80
    cap_bucket_count: int = 3
    liquidity_bucket_count: int = 3


def _import_pykrx():
    try:
        from pykrx import stock
        import pykrx
    except Exception as ex:
        raise RuntimeError(
            "pykrx가 필요합니다. requirements.txt 설치 후 다시 실행하세요. "
            f"원인: {type(ex).__name__}: {ex}"
        ) from ex
    return stock, getattr(pykrx, "__version__", "unknown")


def _to_yyyymmdd(x) -> str:
    return pd.Timestamp(x).strftime("%Y%m%d")


def resolve_reference_business_day(ref_date) -> str:
    stock, _ = _import_pykrx()
    return stock.get_nearest_business_day_in_a_week(_to_yyyymmdd(ref_date), prev=True)


def _normalize_sector_frame(df: pd.DataFrame, market: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["ticker", "name", "sector", "close", "market_cap", "market"])
    x = df.copy().reset_index()
    ren = {
        x.columns[0]: "ticker",
        "종목명": "name",
        "업종명": "sector",
        "종가": "close",
        "시가총액": "market_cap",
    }
    x = x.rename(columns=ren)
    keep = [c for c in ["ticker", "name", "sector", "close", "market_cap"] if c in x.columns]
    x = x[keep].copy()
    x["ticker"] = x["ticker"].astype(str).str.zfill(6)
    x["market"] = market
    for c in ["close", "market_cap"]:
        if c in x:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def _normalize_cap_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["ticker", "volume", "trading_value", "listed_shares", "market_cap_capapi"])
    x = df.copy().reset_index()
    x = x.rename(columns={
        x.columns[0]: "ticker",
        "거래량": "volume",
        "거래대금": "trading_value",
        "상장주식수": "listed_shares",
        "시가총액": "market_cap_capapi",
        "종가": "close_capapi",
    })
    x["ticker"] = x["ticker"].astype(str).str.zfill(6)
    for c in ["volume", "trading_value", "listed_shares", "market_cap_capapi", "close_capapi"]:
        if c in x:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def get_prior_business_days(ref_yyyymmdd: str, lookback_sessions: int, calendar_days: int = 80) -> list[str]:
    stock, _ = _import_pykrx()
    end = pd.Timestamp(ref_yyyymmdd)
    start = end - pd.Timedelta(days=calendar_days)
    days = stock.get_previous_business_days(
        fromdate=start.strftime("%Y%m%d"),
        todate=end.strftime("%Y%m%d"),
    )
    days = [pd.Timestamp(x) for x in days if pd.Timestamp(x) <= end]
    if len(days) < lookback_sessions:
        raise RuntimeError(
            f"영업일 {lookback_sessions}개가 필요하지만 {len(days)}개만 확보했습니다. "
            "business_day_calendar_days를 늘리세요."
        )
    return [d.strftime("%Y%m%d") for d in days[-lookback_sessions:]]


def fetch_snapshot(ref_date, config: UniverseConfig = UniverseConfig(), progress=None):
    stock, pykrx_version = _import_pykrx()
    ref = resolve_reference_business_day(ref_date)
    prior_days = get_prior_business_days(
        ref, config.liquidity_lookback_sessions, config.business_day_calendar_days
    )

    frames = []
    diagnostics = {
        "requested_reference_date": _to_yyyymmdd(ref_date),
        "resolved_reference_business_day": ref,
        "pykrx_version": pykrx_version,
        "markets": list(config.markets),
        "liquidity_days": prior_days,
        "errors": [],
        "outcomes_opened": False,
    }

    # Point-in-time sector + market-cap snapshot.
    for market in config.markets:
        try:
            sec = stock.get_market_sector_classifications(ref, market)
            cap = stock.get_market_cap_by_ticker(ref, market)
            a = _normalize_sector_frame(sec, market)
            b = _normalize_cap_frame(cap)
            x = a.merge(b, on="ticker", how="outer", validate="one_to_one")
            x["market"] = x["market"].fillna(market)
            frames.append(x)
        except Exception as ex:
            diagnostics["errors"].append(
                {"stage": "snapshot", "market": market, "error": f"{type(ex).__name__}: {ex}"}
            )

    if not frames:
        return pd.DataFrame(), diagnostics

    snap = pd.concat(frames, ignore_index=True)
    snap = snap.drop_duplicates(["market", "ticker"], keep="first")

    # Prior 20 valid market sessions' daily trading value. One request per date/market.
    tv_records = []
    total = max(1, len(prior_days) * len(config.markets))
    done = 0
    for d in prior_days:
        for market in config.markets:
            done += 1
            if progress:
                progress(done / total, f"{market} {d} 거래대금")
            try:
                q = stock.get_market_ohlcv_by_ticker(d, market)
                if q is None or q.empty:
                    continue
                q = q.copy().reset_index()
                q = q.rename(columns={q.columns[0]: "ticker", "거래대금": "trading_value_day"})
                if "trading_value_day" not in q:
                    continue
                q["ticker"] = q["ticker"].astype(str).str.zfill(6)
                q["trading_value_day"] = pd.to_numeric(q["trading_value_day"], errors="coerce")
                q["market"] = market
                q["date"] = d
                tv_records.append(q[["market", "ticker", "date", "trading_value_day"]])
            except Exception as ex:
                diagnostics["errors"].append(
                    {"stage": "liquidity_day", "market": market, "date": d,
                     "error": f"{type(ex).__name__}: {ex}"}
                )

    if tv_records:
        tv = pd.concat(tv_records, ignore_index=True)
        liq = tv.groupby(["market", "ticker"], as_index=False).agg(
            median_trading_value_20d=("trading_value_day", "median"),
            trading_value_obs_20d=("trading_value_day", "count"),
        )
        snap = snap.merge(liq, on=["market", "ticker"], how="left")
    else:
        snap["median_trading_value_20d"] = np.nan
        snap["trading_value_obs_20d"] = 0

    snap["reference_date"] = pd.to_datetime(ref, format="%Y%m%d")
    snap["source"] = "PYKRX_KRX_POINT_IN_TIME"

    return snap.sort_values(["market", "ticker"]).reset_index(drop=True), diagnostics


def classify_common_stock_candidates(snapshot: pd.DataFrame) -> pd.DataFrame:
    """
    Conservative, transparent screening.
    KRX stock market APIs already exclude ETF/ETN; preferred shares and SPACs
    are screened by explicit name patterns and remain visible in the full snapshot.

    This is not a claim that name-pattern classification is perfect.
    Rows carry classification_method and review flags for audit.
    """
    x = snapshot.copy()
    x["name"] = x.get("name", pd.Series(index=x.index, dtype=object)).fillna("").astype(str)
    x["is_preferred_name_pattern"] = x["name"].str.contains(PREFERRED_NAME_RE, na=False)
    x["is_spac_name_pattern"] = x["name"].str.contains(SPAC_NAME_RE, na=False)
    x["classification_method"] = "KRX_STOCK_API_PLUS_CONSERVATIVE_NAME_PATTERN"
    x["classification_review_flag"] = np.where(
        x["is_preferred_name_pattern"] | x["is_spac_name_pattern"],
        "EXCLUDED_SPECIAL_OR_PREFERRED",
        "COMMON_STOCK_CANDIDATE_UNVERIFIED_SECURITY_CLASS"
    )

    required_numeric = (
        pd.to_numeric(x.get("market_cap"), errors="coerce").fillna(0) > 0
    )
    x["eligible_common_candidate"] = (
        x["market"].isin(["KOSPI", "KOSDAQ"])
        & ~x["is_preferred_name_pattern"]
        & ~x["is_spac_name_pattern"]
        & required_numeric
    )
    return x


def _rank_bucket(series: pd.Series, n: int, labels: list[str]) -> pd.Series:
    # deterministic rank percentile buckets, robust to ties
    s = pd.to_numeric(series, errors="coerce")
    ranks = s.rank(method="first", ascending=False)
    count = s.notna().sum()
    if count == 0:
        return pd.Series(["UNKNOWN"] * len(s), index=s.index)
    pct = (ranks - 1) / max(count, 1)
    idx = np.minimum((pct * n).astype("Int64"), n - 1)
    out = pd.Series("UNKNOWN", index=s.index, dtype=object)
    for i, label in enumerate(labels):
        out.loc[idx == i] = label
    return out


def add_structural_strata(snapshot: pd.DataFrame, config: UniverseConfig = UniverseConfig()) -> pd.DataFrame:
    x = snapshot.copy()

    cap_labels = ["CAP_LARGE", "CAP_MID", "CAP_SMALL"]
    liq_labels = ["LIQ_HIGH", "LIQ_MID", "LIQ_LOW"]

    x["market_cap_bucket"] = "UNKNOWN"
    x["liquidity_bucket"] = "UNKNOWN"

    for market, idx in x.groupby("market").groups.items():
        idx = list(idx)
        x.loc[idx, "market_cap_bucket"] = _rank_bucket(
            x.loc[idx, "market_cap"], config.cap_bucket_count, cap_labels
        ).values
        x.loc[idx, "liquidity_bucket"] = _rank_bucket(
            x.loc[idx, "median_trading_value_20d"], config.liquidity_bucket_count, liq_labels
        ).values

    x["sector"] = x.get("sector", pd.Series(index=x.index, dtype=object)).fillna("SECTOR_UNKNOWN")
    x["selection_stratum"] = (
        x["market"].astype(str) + "|" +
        x["sector"].astype(str) + "|" +
        x["market_cap_bucket"].astype(str)
    )

    # Listing-age is intentionally deferred until per-stock OHLCV lineage is collected.
    x["listing_age_bucket"] = "PENDING_OHLCV_HISTORY"
    return x


def deterministic_selection_order(snapshot: pd.DataFrame) -> pd.DataFrame:
    """
    No fixed Development stock count is chosen here.
    Produces a deterministic progressive order:
    within each market|sector|cap stratum sort by stable ticker, then round-robin
    across strata by within-stratum rank.
    """
    x = snapshot[snapshot["eligible_common_candidate"]].copy()
    x = x.sort_values(["selection_stratum", "ticker"]).reset_index(drop=True)
    x["within_stratum_rank"] = x.groupby("selection_stratum").cumcount() + 1

    # Stable stratum order independent of response outcomes.
    strata = sorted(x["selection_stratum"].unique().tolist())
    smap = {s: i for i, s in enumerate(strata)}
    x["stratum_order"] = x["selection_stratum"].map(smap)
    x = x.sort_values(
        ["within_stratum_rank", "stratum_order", "ticker"]
    ).reset_index(drop=True)
    x["development_selection_order"] = np.arange(1, len(x) + 1)
    return x


def build_universe(ref_date, config: UniverseConfig = UniverseConfig(), progress=None):
    raw, diag = fetch_snapshot(ref_date, config, progress=progress)
    if raw.empty:
        return raw, raw, diag
    full = classify_common_stock_candidates(raw)
    full = add_structural_strata(full, config)
    ordered = deterministic_selection_order(full)
    diag["snapshot_rows"] = int(len(full))
    diag["eligible_common_candidates"] = int(full["eligible_common_candidate"].sum())
    diag["selection_order_rows"] = int(len(ordered))
    diag["selection_rule"] = (
        "market(KOSPI/KOSDAQ)+KRX sector+within-market cap tercile; "
        "ticker-stable round-robin; no fixed N; no response outcomes"
    )
    return full, ordered, diag
