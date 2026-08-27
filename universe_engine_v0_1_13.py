# HRF Track 02 — Point-in-Time Development Universe Engine v0.1.13
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



def _fdr_desc_snapshot(markets: tuple[str, ...], diagnostics: dict | None = None) -> pd.DataFrame:
    """
    Current-date descriptive listing used for sector, listing date, and
    common-stock company cross-check. Current reference date only.
    """
    fdr = _import_fdr()
    pieces = []
    for market in markets:
        symbol = f"{market}-DESC"
        try:
            df = fdr.StockListing(symbol)
            if diagnostics is not None:
                diagnostics.setdefault("fdr_desc_listing", []).append({
                    "market": market,
                    "symbol": symbol,
                    "rows": 0 if df is None else int(len(df)),
                    "columns": [] if df is None else [str(c) for c in df.columns],
                })
            if df is None or df.empty:
                continue

            x = df.copy()
            code_col = next((c for c in ["Code", "Symbol", "종목코드"] if c in x.columns), None)
            name_col = next((c for c in ["Name", "종목명"] if c in x.columns), None)
            sector_col = next((c for c in ["Sector", "업종"] if c in x.columns), None)
            industry_col = next((c for c in ["Industry", "주요제품"] if c in x.columns), None)
            listing_col = next((c for c in ["ListingDate", "상장일"] if c in x.columns), None)
            if code_col is None:
                continue

            y = pd.DataFrame(index=x.index)
            y["ticker"] = (
                x[code_col].astype(str)
                .str.extract(r"(\d{6})", expand=False)
                .fillna(x[code_col].astype(str))
                .str.zfill(6)
            )
            y["market"] = market
            y["desc_name"] = x[name_col].astype(str) if name_col else ""
            y["desc_sector"] = x[sector_col].astype(str) if sector_col else "SECTOR_UNKNOWN"
            y["desc_industry"] = x[industry_col].astype(str) if industry_col else ""
            y["listing_date"] = pd.to_datetime(x[listing_col], errors="coerce") if listing_col else pd.NaT
            y = y[y["ticker"].str.fullmatch(r"\d{6}", na=False)].copy()
            pieces.append(y)
        except Exception as ex:
            if diagnostics is not None:
                diagnostics.setdefault("errors", []).append({
                    "stage": "desc_listing",
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
        "engine_build": "UNIVERSE_ENGINE_v0.1.13_20260827",
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

    # Current-date descriptive metadata enrichment.
    # KOSPI-DESC/KOSDAQ-DESC supply sector and listing date and act as a
    # common-stock company cross-check. Do not use current DESC for historical dates.
    if ref_ts.normalize() == today_ts.normalize():
        desc = _fdr_desc_snapshot(config.markets, diagnostics=diagnostics)
        if desc is not None and not desc.empty:
            snap = snap.merge(desc, on=["market", "ticker"], how="left")
            snap["name"] = snap["desc_name"].where(
                snap["desc_name"].notna() & snap["desc_name"].ne("nan") & snap["desc_name"].ne(""),
                snap.get("name", "")
            )
            snap["sector"] = snap["desc_sector"].where(
                snap["desc_sector"].notna() & snap["desc_sector"].ne("nan") & snap["desc_sector"].ne(""),
                "SECTOR_UNKNOWN"
            )
            snap["industry"] = snap["desc_industry"].fillna("")
            snap["in_desc_common_company_list"] = snap["desc_name"].notna()
            snap = snap.drop(columns=["desc_name", "desc_sector", "desc_industry"])
            diagnostics["sector_source"] = "FDR_KOSPI_KOSDAQ_DESC_SAME_DAY"
            diagnostics["common_stock_crosscheck_source"] = "FDR_DESC_MEMBERSHIP"
        else:
            snap["sector"] = "SECTOR_UNKNOWN"
            snap["industry"] = ""
            snap["listing_date"] = pd.NaT
            snap["in_desc_common_company_list"] = False
            diagnostics["sector_source"] = "UNAVAILABLE"
            diagnostics["common_stock_crosscheck_source"] = "UNAVAILABLE"
    else:
        if "name" not in snap:
            snap["name"] = ""
        if "sector" not in snap:
            snap["sector"] = "SECTOR_UNKNOWN"
        if "listing_date" not in snap:
            snap["listing_date"] = pd.NaT
        snap["industry"] = ""
        snap["in_desc_common_company_list"] = False
        diagnostics["sector_source"] = "HISTORICAL_DESC_NOT_USED"
        diagnostics["common_stock_crosscheck_source"] = "HISTORICAL_DESC_NOT_USED"

    if "name" not in snap:
        snap["name"] = ""
    snap["name"] = snap["name"].fillna("").astype(str)
    snap.loc[snap["name"].eq(""), "name"] = snap.loc[snap["name"].eq(""), "ticker"]
    snap["sector"] = snap["sector"].fillna("SECTOR_UNKNOWN").astype(str)

    # Liquidity v0.1.7 policy:
    # pykrx 1.2.8 all-market OHLCV cross-section is broken in deployment.
    # Do NOT fabricate or substitute current-day Amount for the frozen 20-session median.
    # Compute prior-20 median trading value later from each selected stock's own
    # validated OHLCV before the stock becomes Development-eligible.
    snap["median_trading_value_20d"] = np.nan
    snap["trading_value_obs_20d"] = 0
    snap["liquidity_status"] = "PENDING_SELECTED_STOCK_OHLCV_20D"
    diagnostics["liquidity_policy"] = (
        "DEFERRED_TO_SELECTED_STOCK_OHLCV; no current-day substitution; "
        "no pykrx all-market cross-section"
    )
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
    desc_gate = (
        x["in_desc_common_company_list"].fillna(False)
        if "in_desc_common_company_list" in x.columns
        else pd.Series(True, index=x.index)
    )
    x["eligible_common_candidate"] = (
        x["market"].isin(["KOSPI", "KOSDAQ"])
        & ~x["is_preferred_name_pattern"]
        & ~x["is_spac_name_pattern"]
        & required_numeric
        & desc_gate
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



def derive_selection_sector(industry: pd.Series) -> pd.Series:
    """
    Outcome-blind broad-sector mapping from FDR descriptive `Industry`.

    `Sector` in KOSDAQ-DESC is a listing-board classification
    (e.g. 우량기업부/벤처기업부), so it must NOT be used as the economic sector
    for Development stratification.

    This mapper deliberately uses broad keyword groups to avoid hundreds of
    ultra-sparse detailed-industry strata. Unmatched rows remain OTHER_UNKNOWN
    and are never silently dropped.
    """
    s = industry.fillna("").astype(str)

    rules = [
        ("FINANCE", r"금융|은행|증권|보험|신탁|여신|카드|투자"),
        ("BIO_HEALTHCARE", r"의약|바이오|의료|병원|헬스|진단|제약"),
        ("SEMICON_ELECTRONICS", r"반도체|전자부품|컴퓨터|통신 및 방송 장비|디스플레이|전기장비|전동기|발전기|전기 변환"),
        ("SOFTWARE_IT", r"소프트웨어|프로그래밍|시스템 통합|정보서비스|데이터|플랫폼|인터넷|게임"),
        ("AUTO_TRANSPORT_EQUIP", r"자동차|자동차 신품 부품|선박|항공기|철도장비|운송장비"),
        ("INDUSTRIAL_MACHINERY", r"기계 제조|특수 목적용 기계|일반 목적용 기계|산업용 로봇|공작기계"),
        ("CHEMICAL_MATERIALS", r"화학|플라스틱|고무|유리|세라믹|비금속 광물|펄프|종이"),
        ("METALS", r"철강|금속|알루미늄|비철금속|금속 가공"),
        ("ENERGY_UTILITIES", r"전기|가스|증기|에너지|석유|연료|발전"),
        ("CONSTRUCTION_REAL_ESTATE", r"건설|토목|건축|부동산"),
        ("CONSUMER_FOOD_RETAIL", r"식품|음료|담배|의복|섬유|가죽|화장품|생활용품|소매|도매|유통"),
        ("MEDIA_ENTERTAINMENT", r"영상|방송|음악|엔터테인먼트|출판|광고|영화"),
        ("TRANSPORT_LOGISTICS", r"운송|창고|물류|해상|항공 운송|택배"),
        ("PROFESSIONAL_SERVICES", r"연구개발|전문 서비스|기술 시험|경영 컨설팅|법무|회계"),
        ("LEISURE_HOSPITALITY", r"숙박|음식점|여행|레저|스포츠|오락"),
        ("AGRI_ENVIRONMENT", r"농업|임업|어업|폐기물|환경|재활용"),
    ]

    out = pd.Series("OTHER_UNKNOWN", index=s.index, dtype=object)
    for label, pattern in rules:
        mask = out.eq("OTHER_UNKNOWN") & s.str.contains(pattern, regex=True, na=False)
        out.loc[mask] = label

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

    # Keep raw FDR fields for audit.
    x["listing_board_sector"] = (
        x.get("sector", pd.Series(index=x.index, dtype=object))
        .fillna("SECTOR_UNKNOWN")
        .astype(str)
    )
    x["industry"] = (
        x.get("industry", pd.Series(index=x.index, dtype=object))
        .fillna("")
        .astype(str)
    )

    # Development selection uses broad economic sector derived from Industry,
    # NOT the KOSDAQ listing-board `Sector` field.
    x["selection_sector"] = derive_selection_sector(x["industry"])

    x["selection_stratum"] = (
        x["market"].astype(str) + "|" +
        x["selection_sector"].astype(str) + "|" +
        x["market_cap_bucket"].astype(str)
    )

    if "listing_date" in x.columns:
        ref = pd.to_datetime(x.get("reference_date"), errors="coerce")
        ld = pd.to_datetime(x["listing_date"], errors="coerce")
        age_days = (ref - ld).dt.days
        x["listing_age_bucket"] = np.select(
            [
                age_days < 365 * 3,
                (age_days >= 365 * 3) & (age_days < 365 * 10),
                age_days >= 365 * 10,
            ],
            ["AGE_LT3Y", "AGE_3_10Y", "AGE_GE10Y"],
            default="PENDING_OHLCV_HISTORY",
        )
    else:
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
        "market(KOSPI/KOSDAQ)+FDR Industry-derived broad economic sector+within-market cap tercile; "
        "ticker-stable round-robin; no fixed N; no response outcomes"
    )
    return full, ordered, diag
