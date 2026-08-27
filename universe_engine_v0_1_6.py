# HRF Track 02 — Point-in-Time Development Universe Engine v0.1.6
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


def _import_fdr():
    try:
        import FinanceDataReader as fdr
    except Exception as ex:
        raise RuntimeError(
            "FinanceDataReader가 필요합니다. requirements.txt 설치 후 다시 실행하세요. "
            f"원인: {type(ex).__name__}: {ex}"
        ) from ex
    return fdr


def _fdr_kospi_calendar(ref_date, calendar_days: int = 120) -> list[str]:
    """
    Use the already-approved FDR KOSPI index path only as a trading-calendar source.
    No future response outcome is accessed.
    """
    fdr = _import_fdr()
    end = pd.Timestamp(ref_date)
    start = end - pd.Timedelta(days=calendar_days)
    df = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return []
    idx = pd.to_datetime(df.index, errors="coerce")
    idx = idx[(~pd.isna(idx)) & (idx <= end)]
    return [pd.Timestamp(x).strftime("%Y%m%d") for x in idx]


def resolve_reference_business_day(ref_date) -> str:
    """
    v0.1.1 fix:
    pykrx.get_nearest_business_day_in_a_week can throw IndexError when its
    internal date query returns an empty array. Prefer the FDR KOSPI calendar,
    which is already used successfully by this app for market-index dates.
    Fall back to direct pykrx market-cap probes, never to a guessed weekday.
    """
    days = _fdr_kospi_calendar(ref_date, calendar_days=20)
    if days:
        return days[-1]

    stock, _ = _import_pykrx()
    end = pd.Timestamp(ref_date)
    errors = []
    for back in range(0, 15):
        d = (end - pd.Timedelta(days=back)).strftime("%Y%m%d")
        try:
            q = stock.get_market_cap_by_ticker(d, "KOSPI")
            if q is not None and not q.empty:
                return d
        except Exception as ex:
            errors.append(f"{d}:{type(ex).__name__}")
    raise RuntimeError(
        "기준 영업일을 확인하지 못했습니다. FDR KOSPI calendar와 pykrx KOSPI "
        f"market-cap probe가 모두 실패했습니다. recent={errors[:5]}"
    )


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
    """
    Schema-flexible market snapshot normalizer.

    pykrx versions/endpoints can expose either Korean or English-like column names,
    and some versions return only a subset. Never select a fixed set of Korean
    columns before checking what is actually present.
    """
    cols = [
        "ticker", "close_capapi", "volume", "trading_value",
        "listed_shares", "market_cap_capapi"
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    x = df.copy().reset_index()
    first = x.columns[0]
    x = x.rename(columns={first: "ticker"})

    aliases = {
        "close_capapi": ["종가", "Close", "close"],
        "volume": ["거래량", "Volume", "volume"],
        "trading_value": ["거래대금", "Amount", "TradingValue", "Value", "amount", "trading_value"],
        "listed_shares": ["상장주식수", "Stocks", "Shares", "listed_shares"],
        "market_cap_capapi": ["시가총액", "Marcap", "MarketCap", "market_cap", "marcap"],
    }

    for target, choices in aliases.items():
        found = next((c for c in choices if c in x.columns), None)
        if found is not None and found != target:
            x = x.rename(columns={found: target})
        if target not in x.columns:
            x[target] = np.nan

    x["ticker"] = x["ticker"].astype(str).str.extract(r"(\d{6})", expand=False).fillna(x["ticker"].astype(str)).str.zfill(6)
    for c in cols[1:]:
        x[c] = pd.to_numeric(x[c], errors="coerce")

    return x[cols].copy()


def get_prior_business_days(ref_yyyymmdd: str, lookback_sessions: int, calendar_days: int = 80) -> list[str]:
    """
    Obtain actual KOSPI trading dates from FDR instead of pykrx helper
    get_previous_business_days, avoiding the same empty-index failure mode.
    """
    end = pd.Timestamp(ref_yyyymmdd)
    days = _fdr_kospi_calendar(end, calendar_days=max(calendar_days, 120))
    days = [d for d in days if d <= ref_yyyymmdd]
    if len(days) < lookback_sessions:
        raise RuntimeError(
            f"KOSPI 실제 거래일 {lookback_sessions}개가 필요하지만 {len(days)}개만 확보했습니다. "
            "business_day_calendar_days를 늘리거나 FDR 상태를 확인하세요."
        )
    return days[-lookback_sessions:]



def _normalize_fdr_listing_frame(x: pd.DataFrame, forced_market: str) -> pd.DataFrame:
    if x is None or x.empty:
        return pd.DataFrame()

    aliases = {
        "ticker": ["Code", "Symbol", "code", "종목코드"],
        "name": ["Name", "name", "종목명"],
        "sector": ["Sector", "Industry", "sector", "업종"],
        "close_capapi": ["Close", "close", "종가"],
        "volume": ["Volume", "volume", "거래량"],
        "trading_value": ["Amount", "TradingValue", "Value", "amount", "거래대금"],
        "market_cap_capapi": ["Marcap", "MarketCap", "market_cap", "시가총액"],
        "listed_shares": ["Stocks", "Shares", "listed_shares", "상장주식수"],
    }

    out = pd.DataFrame(index=x.index)
    for target, choices in aliases.items():
        found = next((c for c in choices if c in x.columns), None)
        out[target] = x[found] if found is not None else np.nan

    out["ticker"] = (
        out["ticker"].astype(str)
        .str.extract(r"(\d{6})", expand=False)
        .fillna(out["ticker"].astype(str))
        .str.zfill(6)
    )
    out["market"] = forced_market
    out["name"] = out["name"].fillna("").astype(str)
    out["sector"] = out["sector"].fillna("SECTOR_UNKNOWN").astype(str)

    for c in ["close_capapi", "volume", "trading_value", "market_cap_capapi", "listed_shares"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    # Require a usable ticker and positive current market cap for the selection frame.
    out = out[out["ticker"].str.fullmatch(r"\d{6}", na=False)].copy()
    return out.reset_index(drop=True)


def _fdr_current_snapshot(markets: tuple[str, ...], diagnostics: dict | None = None) -> pd.DataFrame:
    """
    Current-date snapshot via separate FDR market listings.
    Using separate KOSPI/KOSDAQ requests is more robust than StockListing('KRX')
    on the deployed FinanceDataReader/Streamlit combination.
    """
    fdr = _import_fdr()
    pieces = []

    for market in markets:
        try:
            listing = fdr.StockListing(market)
            if diagnostics is not None:
                diagnostics.setdefault("fdr_listing", []).append({
                    "market": market,
                    "rows": 0 if listing is None else int(len(listing)),
                    "columns": [] if listing is None else [str(c) for c in listing.columns],
                })
            y = _normalize_fdr_listing_frame(listing, market)
            if not y.empty:
                pieces.append(y)
        except Exception as ex:
            if diagnostics is not None:
                diagnostics.setdefault("errors", []).append({
                    "stage": "snapshot_fdr_market",
                    "market": market,
                    "error": f"{type(ex).__name__}: {ex}",
                })

    if not pieces:
        return pd.DataFrame()

    return (
        pd.concat(pieces, ignore_index=True)
        .drop_duplicates(["market", "ticker"], keep="first")
        .reset_index(drop=True)
    )


def fetch_snapshot(ref_date, config: UniverseConfig = UniverseConfig(), progress=None):
    stock, pykrx_version = _import_pykrx()
    ref = resolve_reference_business_day(ref_date)
    prior_days = get_prior_business_days(
        ref, config.liquidity_lookback_sessions, config.business_day_calendar_days
    )

    frames = []
    diagnostics = {
        "engine_build": "UNIVERSE_ENGINE_v0.1.6_20260827",
        "requested_reference_date": _to_yyyymmdd(ref_date),
        "resolved_reference_business_day": ref,
        "pykrx_version": pykrx_version,
        "markets": list(config.markets),
        "liquidity_days": prior_days,
        "errors": [],
        "outcomes_opened": False,
    }

    # Point-in-time market snapshot.
    # v0.1.3: for the current reference date, use FDR KRX listing first because
    # the deployed pykrx 1.2.8 snapshot APIs returned schema-incompatible frames.
    # For historical reference dates, current FDR listing is forbidden.
    ref_ts = pd.Timestamp(ref)
    today_ts = pd.Timestamp(date.today().strftime("%Y%m%d"))

    if ref_ts.normalize() == today_ts.normalize():
        fdr_snap = _fdr_current_snapshot(config.markets, diagnostics=diagnostics)
        if fdr_snap is not None and not fdr_snap.empty:
            frames.append(fdr_snap)
            diagnostics["snapshot_source"] = "FDR_KOSPI_KOSDAQ_LISTINGS_SAME_DAY"
        else:
            diagnostics["errors"].append({
                "stage": "snapshot_fdr_current",
                "error": "FDR KOSPI/KOSDAQ listings returned no usable rows",
            })
    else:
        # Historical reference dates cannot use current FDR listings.
        # Keep pykrx as a historical-only fallback; do not contaminate point-in-time data.
        for market in config.markets:
            try:
                cap = stock.get_market_cap_by_ticker(ref, market)
                b = _normalize_cap_frame(cap)
                if b.empty:
                    raise RuntimeError("empty market-cap snapshot")
                b["market"] = market
                b["name"] = ""
                b["sector"] = "SECTOR_UNKNOWN"
                frames.append(b)
            except Exception as ex:
                diagnostics["errors"].append(
                    {"stage": "snapshot_cap_historical", "market": market,
                     "error": f"{type(ex).__name__}: {ex}"}
                )
    if not frames:
        diagnostics["snapshot_failure"] = (
            "No usable current KOSPI/KOSDAQ listing snapshot. "
            "Inspect fdr_listing rows/columns and errors."
        )
        return pd.DataFrame(), diagnostics

    snap = pd.concat(frames, ignore_index=True)
    snap = snap.drop_duplicates(["market", "ticker"], keep="first")

    # Name + sector enrichment.
    # If FDR current listing already supplied these fields, keep them.
    # Otherwise try pykrx sector metadata as a non-fatal enrichment.
    if diagnostics.get("snapshot_source") != "FDR_KOSPI_KOSDAQ_LISTINGS_SAME_DAY":
        sector_pieces = []
        for market in config.markets:
            try:
                sec = stock.get_market_sector_classifications(ref, market)
                a = _normalize_sector_frame(sec, market)
                if not a.empty:
                    sector_pieces.append(a[["ticker", "name", "sector", "market"]])
            except Exception as ex:
                diagnostics["errors"].append(
                    {"stage": "sector_primary", "market": market, "error": f"{type(ex).__name__}: {ex}"}
                )

        if sector_pieces:
            sec_all = pd.concat(sector_pieces, ignore_index=True).drop_duplicates(["market", "ticker"])
            snap = snap.merge(sec_all, on=["market", "ticker"], how="left", suffixes=("", "_sec"))
            if "name_sec" in snap.columns:
                snap["name"] = snap["name_sec"].fillna(snap.get("name", ""))
                snap = snap.drop(columns=["name_sec"])
            if "sector_sec" in snap.columns:
                snap["sector"] = snap["sector_sec"].fillna(snap.get("sector", "SECTOR_UNKNOWN"))
                snap = snap.drop(columns=["sector_sec"])
            diagnostics["sector_source"] = "PYKRX_POINT_IN_TIME"
        else:
            diagnostics["sector_source"] = "UNAVAILABLE"
    else:
        diagnostics["sector_source"] = "FDR_KOSPI_KOSDAQ_LISTINGS_SAME_DAY"

    if "name" not in snap:
        snap["name"] = ""
    snap["name"] = snap["name"].fillna("").astype(str)
    snap.loc[snap["name"].eq(""), "name"] = snap.loc[snap["name"].eq(""), "ticker"]

    if "sector" not in snap:
        snap["sector"] = "SECTOR_UNKNOWN"
    snap["sector"] = snap["sector"].fillna("SECTOR_UNKNOWN").astype(str)

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
                q = q.rename(columns={q.columns[0]: "ticker"})
                value_col = next(
                    (c for c in ["거래대금", "Amount", "TradingValue", "Value", "amount"] if c in q.columns),
                    None,
                )
                if value_col is None:
                    diagnostics["errors"].append(
                        {"stage": "liquidity_schema", "market": market, "date": d,
                         "error": f"no trading-value column; columns={list(q.columns)}"}
                    )
                    continue
                q = q.rename(columns={value_col: "trading_value_day"})
                q["ticker"] = q["ticker"].astype(str).str.extract(r"(\d{6})", expand=False).fillna(q["ticker"].astype(str)).str.zfill(6)
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

    cap_series = x["market_cap"] if "market_cap" in x.columns else x.get("market_cap_capapi")
    required_numeric = pd.to_numeric(cap_series, errors="coerce").fillna(0) > 0
    if "market_cap" not in x.columns and "market_cap_capapi" in x.columns:
        x["market_cap"] = pd.to_numeric(x["market_cap_capapi"], errors="coerce")
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

    # v0.1.6 fix:
    # pandas nullable Int64 refuses a safe cast from non-integral floats such as
    # 0.37 or 1.82 ("cannot safely cast non-equivalent object to int64").
    # Bucket indices must be explicitly floored before integer conversion.
    raw_idx = np.floor(pd.to_numeric(pct, errors="coerce") * n)
    raw_idx = raw_idx.clip(lower=0, upper=n - 1)
    idx = pd.Series(raw_idx, index=s.index).astype("Int64")

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
