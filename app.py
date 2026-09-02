import re
import time
import io
import json
import zipfile
import hashlib
import math
import threading
import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

from universe_engine_v0_1_14 import (
    UniverseConfig,
    add_structural_strata,
    build_universe,
    classify_common_stock_candidates,
    deterministic_selection_order,
)

st.set_page_config(page_title="Korea OHLCV CSV v1.0.18 UNIVERSE + FLOW 99", page_icon="📈")

H = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 자주 쓰는 종목은 네이버 자동완성 상태와 무관하게 즉시 해석한다.
ALIASES = {
    "펩트론": "087010",
    "삼성전자": "005930",
    "디앤디파마텍": "347850",
    "코오롱티슈진": "950160",
    "SK하이닉스": "000660",
    "에스케이하이닉스": "000660",
    "하이닉스": "000660",
    "하이닉스반도체": "000660",
}
REV = {
    "087010": "펩트론",
    "005930": "삼성전자",
    "347850": "디앤디파마텍",
    "950160": "코오롱티슈진",
    "000660": "SK하이닉스",
}

# KRX issue code(ISIN) fallback.
# 000660은 SK hynix 공식 IR에서 확인한 현재 ISIN.
KNOWN_ISIN = {
    "000660": "KR7000660001",
}

KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
KRX_LOGIN_JSP = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
KRX_LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
KRX_REFERER = "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd"

FLOW_ENGINE_VERSION = "INVESTOR_FLOW_INPUT_v0.3.0_20260902"
FLOW_BATCH_ENGINE_VERSION = "FLOW_BATCH_v0.2.0_20260902"
UNIVERSE_DIRECT_ENGINE_VERSION = "UNIVERSE_DIRECT_v0.1.0_20260902"
FLOW_BATCH_TARGET_COUNT = 99
FLOW_BATCH_CHECKPOINT_ROOT = Path(tempfile.gettempdir()) / "hrf_flow_batch_v1_0_18"
UNIVERSE_MARKET_IDS = {"KOSPI": "STK", "KOSDAQ": "KSQ"}
FLOW_SOURCE = "KRX_VIA_PYKRX_INVESTOR_BY_DATE"
FLOW_INVESTOR_ALIASES = {
    "institution": ("기관합계", "기관", "institution", "institution_total"),
    "foreign": ("외국인합계", "외국인", "foreign", "foreign_total"),
    "individual": ("개인", "individual", "retail"),
    "other_corporation": ("기타법인", "other_corporation", "corporation"),
}
FLOW_SIDES = {"buy": "매수", "sell": "매도", "net": None}
FLOW_METRICS = ("qty", "value")
FLOW_CORE_INVESTORS = ("institution", "foreign")
FLOW_ALL_INVESTORS = ("institution", "foreign", "individual", "other_corporation")
FLOW_AUTH_LOCK = threading.Lock()


@st.cache_data(ttl=3600, show_spinner=False)
def resolve(q):
    q = (q or "").strip()
    if re.fullmatch(r"\d{6}", q):
        return {"name": REV.get(q, q), "code": q}

    key = re.sub(r"\s+", "", q).upper()
    for n, c in ALIASES.items():
        if re.sub(r"\s+", "", n).upper() == key:
            return {"name": REV.get(c, n), "code": c}

    # 일반 종목명은 네이버 자동완성을 보조 resolver로 사용한다.
    try:
        r = requests.get(
            "https://ac.finance.naver.com/ac",
            params={"q": q, "q_enc": "UTF-8", "st": "111", "sug": "all", "frm": "stock"},
            headers=H,
            timeout=15,
        )
        r.raise_for_status()
        text = r.text
        for pat in (
            re.escape(q) + r'.{0,250}?(?:A)?(\d{6})',
            r'(?:A)?(\d{6}).{0,250}?' + re.escape(q),
        ):
            m = re.search(pat, text, re.S | re.I)
            if m:
                code = m.group(1)
                return {"name": REV.get(code, q), "code": code}
    except Exception:
        pass
    return None


def _standardize(df, source):
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "source", "auto_corrected"])

    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    aliases = {
        "날짜": "date", "시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume",
    }
    out = out.rename(columns=aliases)
    need = ["date", "open", "high", "low", "close", "volume"]
    if not all(c in out.columns for c in need):
        raise ValueError(f"필수 OHLCV 열 누락: {out.columns.tolist()}")

    out = out[need].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=need).copy()
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = out[c].astype("int64")
    out["source"] = source
    out["auto_corrected"] = False
    return out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)



def _krx_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": KRX_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }


def _krx_login(session, login_id, login_pw):
    """선택적 KRX 로그인. ID/PW는 저장/CSV출력/로그표시하지 않는다."""
    diag = {"attempted": False, "success": False, "code": "", "message": ""}
    if not (login_id and login_pw):
        return diag

    diag["attempted"] = True
    h = _krx_headers()
    try:
        session.get(KRX_LOGIN_PAGE, headers={"User-Agent": h["User-Agent"]}, timeout=15)
        session.get(
            KRX_LOGIN_JSP,
            headers={"User-Agent": h["User-Agent"], "Referer": KRX_LOGIN_PAGE},
            timeout=15,
        )
        payload = {
            "mbrNm": "", "telNo": "", "di": "", "certType": "",
            "mbrId": login_id, "pw": login_pw,
        }
        r = session.post(
            KRX_LOGIN_URL,
            data=payload,
            headers={"User-Agent": h["User-Agent"], "Referer": KRX_LOGIN_PAGE},
            timeout=20,
        )
        data = r.json()
        code = str(data.get("_error_code", ""))
        msg = str(data.get("_error_message", ""))
        if code == "CD011":  # 중복 로그인
            payload["skipDup"] = "Y"
            r = session.post(
                KRX_LOGIN_URL,
                data=payload,
                headers={"User-Agent": h["User-Agent"], "Referer": KRX_LOGIN_PAGE},
                timeout=20,
            )
            data = r.json()
            code = str(data.get("_error_code", ""))
            msg = str(data.get("_error_message", ""))
        diag.update({"success": code == "CD001", "code": code, "message": msg})
    except Exception as ex:
        diag.update({"success": False, "code": "EXCEPTION", "message": f"{type(ex).__name__}: {ex}"})
    return diag


def _krx_find_isin(session, code):
    # 1) 공식 finder에서 6자리 코드 -> full_code(ISIN)
    payload = {
        "bld": "dbms/comm/finder/finder_stkisu",
        "locale": "ko_KR",
        "mktsel": "ALL",
        "searchText": code,
        "typeNo": "0",
    }
    try:
        r = session.post(KRX_JSON_URL, data=payload, headers=_krx_headers(), timeout=20)
        data = r.json()
        rows = data.get("block1") or []
        for row in rows:
            if str(row.get("short_code", "")).zfill(6) == code:
                full = str(row.get("full_code", "")).strip()
                if full:
                    return full, "FINDER_OK", ""
    except Exception as ex:
        finder_error = f"{type(ex).__name__}: {ex}"
    else:
        finder_error = "finder returned no exact match"

    # 2) 검증된 fallback
    if code in KNOWN_ISIN:
        return KNOWN_ISIN[code], "KNOWN_FALLBACK", finder_error
    return "", "ISIN_NOT_FOUND", finder_error


def _clean_krx_num(v):
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "None", "nan"):
        return None
    return pd.to_numeric(s, errors="coerce")


def _first_existing_column(frame, candidates):
    return next((column for column in candidates if column in frame.columns), None)


def _normalize_krx_universe_price_rows(rows, market):
    """Normalize official KRX MDCSTAT01501 rows without pykrx column assumptions."""
    columns = [
        "ticker", "name", "market", "sector", "close_capapi", "volume",
        "trading_value", "listed_shares", "market_cap_capapi",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    raw = pd.DataFrame(rows)
    aliases = {
        "ticker": ("ISU_SRT_CD", "종목코드", "ticker", "Code"),
        "name": ("ISU_ABBRV", "종목명", "name", "Name"),
        "sector": ("SECT_TP_NM", "소속부", "sector", "Sector"),
        "close_capapi": ("TDD_CLSPRC", "종가", "close", "Close"),
        "volume": ("ACC_TRDVOL", "거래량", "volume", "Volume"),
        "trading_value": ("ACC_TRDVAL", "거래대금", "trading_value", "Amount"),
        "listed_shares": ("LIST_SHRS", "상장주식수", "listed_shares", "Stocks"),
        "market_cap_capapi": ("MKTCAP", "시가총액", "market_cap", "Marcap"),
    }
    out = pd.DataFrame(index=raw.index)
    for target, candidates in aliases.items():
        source = _first_existing_column(raw, candidates)
        out[target] = raw[source] if source is not None else pd.NA

    out["ticker"] = (
        out["ticker"].astype(str).str.extract(r"(\d{6})", expand=False)
        .fillna(out["ticker"].astype(str)).str.zfill(6)
    )
    out["name"] = out["name"].fillna("").astype(str)
    out["sector"] = out["sector"].fillna("SECTOR_UNKNOWN").astype(str)
    out["market"] = str(market)
    numeric = [
        "close_capapi", "volume", "trading_value", "listed_shares",
        "market_cap_capapi",
    ]
    for column in numeric:
        out[column] = out[column].map(_clean_krx_num)
    out = out[
        out["ticker"].str.fullmatch(r"\d{6}", na=False)
        & (pd.to_numeric(out["market_cap_capapi"], errors="coerce").fillna(0) > 0)
    ].copy()
    return out[columns].drop_duplicates(["market", "ticker"], keep="first").reset_index(drop=True)


def _normalize_krx_universe_sector_rows(rows, market):
    """Normalize official KRX MDCSTAT03901 historical sector rows."""
    columns = ["ticker", "market", "sector_name", "industry"]
    if not rows:
        return pd.DataFrame(columns=columns)
    raw = pd.DataFrame(rows)
    ticker_column = _first_existing_column(raw, ("ISU_SRT_CD", "종목코드", "ticker"))
    if ticker_column is None:
        return pd.DataFrame(columns=columns)
    name_column = _first_existing_column(raw, ("ISU_ABBRV", "종목명", "name"))
    industry_column = _first_existing_column(raw, ("IDX_IND_NM", "업종명", "industry"))
    out = pd.DataFrame(index=raw.index)
    out["ticker"] = (
        raw[ticker_column].astype(str).str.extract(r"(\d{6})", expand=False)
        .fillna(raw[ticker_column].astype(str)).str.zfill(6)
    )
    out["market"] = str(market)
    out["sector_name"] = raw[name_column].fillna("").astype(str) if name_column else ""
    out["industry"] = (
        raw[industry_column].fillna("").astype(str) if industry_column else ""
    )
    out = out[out["ticker"].str.fullmatch(r"\d{6}", na=False)].copy()
    return out[columns].drop_duplicates(["market", "ticker"], keep="first").reset_index(drop=True)


def _krx_response_rows(data, preferred_keys):
    if not isinstance(data, dict):
        return []
    first_empty = None
    for key in preferred_keys:
        value = data.get(key)
        if isinstance(value, list):
            if value:
                return value
            if first_empty is None:
                first_empty = value
    return first_empty or []


def fetch_krx_direct_universe_snapshot(
    session,
    reference_date,
    markets=("KOSPI", "KOSDAQ"),
    max_back_days=14,
    progress=None,
):
    """
    Build a point-in-time snapshot from official KRX JSON endpoints.

    The date is probed backwards only until both requested markets have a
    non-empty snapshot. This fixes the pykrx 1.2.8 schema failure while keeping
    historical reference dates historical. No current listing is substituted.
    """
    requested = pd.Timestamp(reference_date).date()
    diagnostics = {
        "engine_build": UNIVERSE_DIRECT_ENGINE_VERSION,
        "requested_reference_date": requested.strftime("%Y%m%d"),
        "markets": list(markets),
        "snapshot_source": "KRX_MDCSTAT01501_DIRECT",
        "sector_source": "KRX_MDCSTAT03901_DIRECT_SAME_REFERENCE_DATE",
        "common_stock_crosscheck_source": (
            "KRX_MDCSTAT01501_STOCK_MARKET_MEMBERSHIP_PLUS_NAME_PATTERN"
        ),
        "snapshot_probes": [],
        "errors": [],
        "credentials_stored": False,
        "outcomes_opened": False,
        "future_outcomes_opened": False,
    }
    price_frames = []
    resolved = None

    for back in range(int(max_back_days) + 1):
        candidate = requested - timedelta(days=back)
        candidate_text = candidate.strftime("%Y%m%d")
        day_frames = []
        day_record = {"date": candidate_text, "markets": []}
        for market in markets:
            market_id = UNIVERSE_MARKET_IDS.get(str(market))
            if not market_id:
                raise ValueError(f"지원하지 않는 Universe 시장: {market}")
            payload = {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
                "mktId": market_id,
                "trdDd": candidate_text,
            }
            record = {"market": market, "rows": 0, "status": ""}
            try:
                response = session.post(
                    KRX_JSON_URL, data=payload, headers=_krx_headers(), timeout=30
                )
                response.raise_for_status()
                data = response.json()
                rows = _krx_response_rows(data, ("output", "block1", "OutBlock_1"))
                normalized = _normalize_krx_universe_price_rows(rows, market)
                record["rows"] = int(len(normalized))
                record["status"] = "OK" if not normalized.empty else "EMPTY"
                record["krx_error_code"] = str(data.get("_error_code", ""))
                if not normalized.empty:
                    day_frames.append(normalized)
            except Exception as ex:
                record["status"] = "ERROR"
                record["error"] = f"{type(ex).__name__}: {ex}"
            day_record["markets"].append(record)
        diagnostics["snapshot_probes"].append(day_record)

        found_markets = {str(frame["market"].iloc[0]) for frame in day_frames if not frame.empty}
        if found_markets == set(map(str, markets)):
            price_frames = day_frames
            resolved = candidate
            break
        if progress:
            progress(0.01, f"Universe 거래일 확인 중 · {candidate_text}")

    if resolved is None or not price_frames:
        diagnostics["snapshot_failure"] = (
            f"최근 {int(max_back_days) + 1}일 안에서 KOSPI/KOSDAQ 동시 스냅샷을 찾지 못했습니다."
        )
        return pd.DataFrame(), diagnostics

    resolved_text = resolved.strftime("%Y%m%d")
    diagnostics["resolved_reference_business_day"] = resolved_text
    snapshot = pd.concat(price_frames, ignore_index=True)

    sector_frames = []
    for market in markets:
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT03901",
            "mktId": UNIVERSE_MARKET_IDS[str(market)],
            "trdDd": resolved_text,
        }
        try:
            response = session.post(
                KRX_JSON_URL, data=payload, headers=_krx_headers(), timeout=30
            )
            response.raise_for_status()
            data = response.json()
            rows = _krx_response_rows(data, ("block1", "output", "OutBlock_1"))
            normalized = _normalize_krx_universe_sector_rows(rows, market)
            diagnostics.setdefault("sector_rows", []).append({
                "market": market,
                "rows": int(len(normalized)),
                "krx_error_code": str(data.get("_error_code", "")),
            })
            if not normalized.empty:
                sector_frames.append(normalized)
        except Exception as ex:
            diagnostics["errors"].append({
                "stage": "sector_direct",
                "market": market,
                "error": f"{type(ex).__name__}: {ex}",
            })

    if sector_frames:
        sectors = pd.concat(sector_frames, ignore_index=True)
        snapshot = snapshot.merge(sectors, on=["market", "ticker"], how="left")
        snapshot["name"] = snapshot["sector_name"].where(
            snapshot["sector_name"].fillna("").ne(""), snapshot["name"]
        )
        snapshot = snapshot.drop(columns=["sector_name"])
        snapshot["industry"] = snapshot["industry"].fillna("").astype(str)
    else:
        diagnostics["sector_source"] = "UNAVAILABLE_DEGRADED_TO_MARKET_CAP_STRATA"
        snapshot["industry"] = ""

    # MDCSTAT01501 is itself the official stock-market membership snapshot.
    # Preferred shares/SPACs remain visible and are conservatively screened by name.
    snapshot["in_desc_common_company_list"] = True
    snapshot["listing_date"] = pd.NaT
    snapshot["median_trading_value_20d"] = pd.NA
    snapshot["trading_value_obs_20d"] = 0
    snapshot["liquidity_status"] = "PENDING_SELECTED_STOCK_OHLCV_20D"
    snapshot["reference_date"] = pd.to_datetime(resolved_text, format="%Y%m%d")
    snapshot["source"] = "KRX_DIRECT_MDCSTAT01501_03901_POINT_IN_TIME"
    diagnostics["liquidity_policy"] = (
        "DEFERRED_TO_SELECTED_STOCK_OHLCV; no current-day substitution"
    )
    diagnostics["snapshot_rows_raw"] = int(len(snapshot))
    return snapshot.sort_values(["market", "ticker"]).reset_index(drop=True), diagnostics


def build_krx_direct_universe(session, reference_date, config=UniverseConfig(), progress=None):
    raw, diagnostics = fetch_krx_direct_universe_snapshot(
        session,
        reference_date,
        markets=config.markets,
        progress=progress,
    )
    if raw.empty:
        return raw, raw, diagnostics
    full = classify_common_stock_candidates(raw)
    full = add_structural_strata(full, config)
    ordered = deterministic_selection_order(full)
    diagnostics["snapshot_rows"] = int(len(full))
    diagnostics["eligible_common_candidates"] = int(full["eligible_common_candidate"].sum())
    diagnostics["selection_order_rows"] = int(len(ordered))
    diagnostics["selection_rule"] = (
        "market(KOSPI/KOSDAQ)+same-date KRX industry broad sector+within-market cap tercile; "
        "ticker-stable round-robin; no response outcomes"
    )
    return full, ordered, diagnostics


def fetch_krx_direct_raw_with_active_session(session, code, start, end):
    """Fetch one stock's non-adjusted OHLCV through the already-authenticated KRX session."""
    ticker = str(code).zfill(6)
    diagnostics = {
        "code": ticker,
        "requested_start": str(start),
        "requested_end": str(end),
        "path": "KRX_ACTIVE_SESSION_MDCSTAT01701",
        "chunks": [],
        "credentials_stored": False,
        "future_outcomes_opened": False,
    }
    isin, isin_status, isin_error = _krx_find_isin(session, ticker)
    diagnostics.update({
        "isin": isin,
        "isin_status": isin_status,
        "isin_error": isin_error,
    })
    if not isin:
        diagnostics["status"] = "ISIN_NOT_FOUND"
        return pd.DataFrame(), diagnostics

    pieces = []
    current = pd.Timestamp(start).date()
    final = pd.Timestamp(end).date()
    while current <= final:
        chunk_end = min(current + timedelta(days=699), final)
        record = {
            "start": str(current),
            "end": str(chunk_end),
            "rows": 0,
            "status": "",
            "error": "",
        }
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT01701",
            "isuCd": isin,
            "strtDd": current.strftime("%Y%m%d"),
            "endDd": chunk_end.strftime("%Y%m%d"),
            "adjStkPrc": "1",
        }
        try:
            response = session.post(
                KRX_JSON_URL, data=payload, headers=_krx_headers(), timeout=30
            )
            response.raise_for_status()
            data = response.json()
            rows = _krx_response_rows(data, ("output", "block1", "OutBlock_1"))
            record["rows"] = int(len(rows))
            record["krx_error_code"] = str(data.get("_error_code", ""))
            if not rows:
                record["status"] = "RAW_EMPTY"
            else:
                raw = pd.DataFrame(rows)
                required = {
                    "TRD_DD": "date",
                    "TDD_OPNPRC": "open",
                    "TDD_HGPRC": "high",
                    "TDD_LWPRC": "low",
                    "TDD_CLSPRC": "close",
                    "ACC_TRDVOL": "volume",
                }
                optional = {
                    "ACC_TRDVAL": "trading_value",
                    "MKTCAP": "market_cap",
                    "LIST_SHRS": "listed_shares",
                    "FLUC_TP_CD": "fluc_type_code",
                    "CMPPREVDD_PRC": "change_price",
                    "FLUC_RT": "change_rate_pct",
                }
                missing = [column for column in required if column not in raw.columns]
                if missing:
                    record["status"] = "SCHEMA_ERROR"
                    record["error"] = f"missing={missing}; columns={raw.columns.tolist()[:40]}"
                else:
                    keep = list(required) + [column for column in optional if column in raw.columns]
                    rename = dict(required)
                    rename.update({column: optional[column] for column in optional if column in raw.columns})
                    normalized = raw[keep].rename(columns=rename)
                    normalized["date"] = pd.to_datetime(
                        normalized["date"], format="%Y/%m/%d", errors="coerce"
                    )
                    for column in [
                        "open", "high", "low", "close", "volume", "trading_value",
                        "market_cap", "listed_shares", "fluc_type_code", "change_price",
                        "change_rate_pct",
                    ]:
                        if column in normalized.columns:
                            normalized[column] = normalized[column].map(_clean_krx_num)
                    pieces.append(normalized)
                    record["status"] = "RAW_OK"
        except Exception as ex:
            record["status"] = "REQUEST_ERROR"
            record["error"] = f"{type(ex).__name__}: {ex}"
        diagnostics["chunks"].append(record)
        current = chunk_end + timedelta(days=1)
        time.sleep(0.20)

    if not pieces:
        diagnostics["status"] = "OHLCV_EMPTY"
        return pd.DataFrame(), diagnostics

    out = (
        pd.concat(pieces, ignore_index=True)
        .dropna(subset=["date", "open", "high", "low", "close", "volume"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    integer_columns = [
        "open", "high", "low", "close", "volume", "trading_value",
        "market_cap", "listed_shares", "fluc_type_code", "change_price",
    ]
    for column in integer_columns:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    if "change_rate_pct" in out.columns:
        out["change_rate_pct"] = pd.to_numeric(
            out["change_rate_pct"], errors="coerce"
        ).astype("Float64")
    out["source"] = "KRX_DIRECT_RAW_ACTIVE_SESSION"
    out["auto_corrected"] = False
    diagnostics["status"] = "OHLCV_OK"
    return out, diagnostics


def fetch_krx_direct_raw(code, start, end, login_id="", login_pw=""):
    """
    KRX Data Marketplace 직접 경로:
      finder_stkisu -> isuCd(ISIN)
      MDCSTAT01701 -> adjStkPrc=1 (단순/비수정 가격)
    700일 단위 분할. 로그인 정보는 메모리에서만 사용하며 저장하지 않는다.
    """
    session = requests.Session()
    h = _krx_headers()

    # 익명 세션도 먼저 warm-up
    try:
        session.get(KRX_REFERER, headers={"User-Agent": h["User-Agent"]}, timeout=15)
    except Exception:
        pass

    login_diag = _krx_login(session, login_id, login_pw)
    isin, isin_status, isin_error = _krx_find_isin(session, code)

    diag = {
        "code": code,
        "requested_start": str(start),
        "requested_end": str(end),
        "login_attempted": login_diag["attempted"],
        "login_success": login_diag["success"],
        "login_code": login_diag["code"],
        "login_message": login_diag["message"],
        "isin": isin,
        "isin_status": isin_status,
        "isin_error": isin_error,
        "chunks": [],
    }
    if not isin:
        return _standardize(pd.DataFrame(), "KRX_DIRECT_RAW"), diag

    pieces = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=699), end)
        rec = {
            "start": str(cur),
            "end": str(chunk_end),
            "http": "",
            "rows": 0,
            "status": "",
            "krx_error_code": "",
            "krx_error_message": "",
            "error": "",
        }
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT01701",
            "isuCd": isin,
            "strtDd": cur.strftime("%Y%m%d"),
            "endDd": chunk_end.strftime("%Y%m%d"),
            "adjStkPrc": "1",  # 1=단순종가(비수정), 2=수정종가
        }
        try:
            r = session.post(KRX_JSON_URL, data=payload, headers=h, timeout=30)
            rec["http"] = str(r.status_code)
            data = r.json()
            rec["krx_error_code"] = str(data.get("_error_code", ""))
            rec["krx_error_message"] = str(data.get("_error_message", ""))
            rows = data.get("output") or []
            rec["rows"] = int(len(rows))
            if rows:
                x = pd.DataFrame(rows)
                rename = {
                    "TRD_DD": "date",
                    "TDD_OPNPRC": "open",
                    "TDD_HGPRC": "high",
                    "TDD_LWPRC": "low",
                    "TDD_CLSPRC": "close",
                    "ACC_TRDVOL": "volume",
                }
                missing = [c for c in rename if c not in x.columns]
                if missing:
                    rec["status"] = "SCHEMA_ERROR"
                    rec["error"] = f"missing={missing}; columns={x.columns.tolist()[:20]}"
                else:
                    x = x[list(rename)].rename(columns=rename)
                    x["date"] = pd.to_datetime(x["date"], format="%Y/%m/%d", errors="coerce")
                    for c in ["open", "high", "low", "close", "volume"]:
                        x[c] = x[c].map(_clean_krx_num)
                    pieces.append(x)
                    rec["status"] = "RAW_OK"
            else:
                if rec["krx_error_code"]:
                    rec["status"] = "KRX_ERROR_RESPONSE"
                elif login_diag["attempted"] and not login_diag["success"]:
                    rec["status"] = "LOGIN_FAILED_EMPTY"
                else:
                    rec["status"] = "RAW_EMPTY"
        except Exception as ex:
            rec["status"] = "REQUEST_ERROR"
            rec["error"] = f"{type(ex).__name__}: {ex}"

        diag["chunks"].append(rec)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.25)

    if not pieces:
        return _standardize(pd.DataFrame(), "KRX_DIRECT_RAW"), diag

    raw = pd.concat(pieces, ignore_index=True)
    raw = raw.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    out = _standardize(raw, "KRX_DIRECT_RAW")
    return out, diag


@st.cache_data(ttl=3600, show_spinner=False)

def fetch_krx_direct_raw_batch(code, start, end, login_id="", login_pw=""):
    """
    Batch Development fetcher.

    Same KRX DIRECT RAW endpoint and non-adjusted price path as the existing
    single-stock downloader, but preserves optional ACC_TRDVAL when present.
    This is structural data only; no H15/MFE/MAE outcome is calculated.
    """
    session = requests.Session()
    h = _krx_headers()

    try:
        session.get(KRX_REFERER, headers={"User-Agent": h["User-Agent"]}, timeout=15)
    except Exception:
        pass

    login_diag = _krx_login(session, login_id, login_pw)
    isin, isin_status, isin_error = _krx_find_isin(session, code)

    diag = {
        "code": code,
        "requested_start": str(start),
        "requested_end": str(end),
        "login_attempted": login_diag["attempted"],
        "login_success": login_diag["success"],
        "isin": isin,
        "isin_status": isin_status,
        "isin_error": isin_error,
        "chunks": [],
        "future_outcomes_opened": False,
    }
    if not isin:
        return pd.DataFrame(), diag

    pieces = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=699), end)
        rec = {
            "start": str(cur),
            "end": str(chunk_end),
            "rows": 0,
            "status": "",
            "error": "",
        }
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT01701",
            "isuCd": isin,
            "strtDd": cur.strftime("%Y%m%d"),
            "endDd": chunk_end.strftime("%Y%m%d"),
            "adjStkPrc": "1",
        }
        try:
            r = session.post(KRX_JSON_URL, data=payload, headers=h, timeout=30)
            data = r.json()
            rows = data.get("output") or []
            rec["rows"] = int(len(rows))
            if not rows:
                rec["status"] = "RAW_EMPTY"
            else:
                x = pd.DataFrame(rows)
                required = {
                    "TRD_DD": "date",
                    "TDD_OPNPRC": "open",
                    "TDD_HGPRC": "high",
                    "TDD_LWPRC": "low",
                    "TDD_CLSPRC": "close",
                    "ACC_TRDVOL": "volume",
                }
                missing = [c for c in required if c not in x.columns]
                if missing:
                    rec["status"] = "SCHEMA_ERROR"
                    rec["error"] = f"missing={missing}; columns={x.columns.tolist()[:30]}"
                else:
                    keep = list(required)
                    if "ACC_TRDVAL" in x.columns:
                        keep.append("ACC_TRDVAL")
                    y = x[keep].rename(columns=required | {"ACC_TRDVAL": "trading_value"})
                    y["date"] = pd.to_datetime(y["date"], format="%Y/%m/%d", errors="coerce")
                    for c in ["open", "high", "low", "close", "volume", "trading_value"]:
                        if c in y.columns:
                            y[c] = y[c].map(_clean_krx_num)
                    pieces.append(y)
                    rec["status"] = "RAW_OK"
        except Exception as ex:
            rec["status"] = "REQUEST_ERROR"
            rec["error"] = f"{type(ex).__name__}: {ex}"

        diag["chunks"].append(rec)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.20)

    if not pieces:
        return pd.DataFrame(), diag

    out = pd.concat(pieces, ignore_index=True)
    need = ["date", "open", "high", "low", "close", "volume"]
    out = out.dropna(subset=need).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")
    if "trading_value" in out.columns:
        out["trading_value"] = pd.to_numeric(out["trading_value"], errors="coerce").astype("Int64")
    else:
        out["trading_value"] = pd.Series(pd.NA, index=out.index, dtype="Int64")

    finite = out[["open", "high", "low", "close"]].notna().all(axis=1)
    out["valid_session"] = (out["volume"].fillna(0) != 0) & finite
    out["source"] = "KRX_DIRECT_RAW_BATCH"
    out["auto_corrected"] = False
    return out, diag




def fetch_krx_direct_raw_diagnostic(code, start, end, login_id="", login_pw=""):
    """KRX request-shape diagnostic mirror. No credentials/outcomes are logged."""
    session = requests.Session()
    h = _krx_headers()

    diag = {
        "code_value": str(code),
        "code_type": type(code).__name__,
        "start_value": str(start),
        "start_type": type(start).__name__,
        "end_value": str(end),
        "end_type": type(end).__name__,
        "header_keys": sorted(list(h.keys())),
        "requests": [],
        "future_outcomes_opened": False,
    }

    try:
        warm = session.get(KRX_REFERER, headers={"User-Agent": h.get("User-Agent", "")}, timeout=15)
        diag["warmup_status"] = int(warm.status_code)
        diag["warmup_content_type"] = warm.headers.get("Content-Type", "")
    except Exception as ex:
        diag["warmup_error"] = f"{type(ex).__name__}: {ex}"

    login_diag = _krx_login(session, login_id, login_pw)
    diag["login_attempted"] = login_diag.get("attempted")
    diag["login_success"] = login_diag.get("success")

    isin, isin_status, isin_error = _krx_find_isin(session, str(code))
    diag["isin"] = isin
    diag["isin_status"] = isin_status
    diag["isin_error"] = isin_error
    if not isin:
        return pd.DataFrame(), diag

    s = pd.Timestamp(start).date()
    e = pd.Timestamp(end).date()
    pieces = []
    cur = s

    while cur <= e:
        chunk_end = min(cur + timedelta(days=699), e)
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT01701",
            "isuCd": isin,
            "strtDd": cur.strftime("%Y%m%d"),
            "endDd": chunk_end.strftime("%Y%m%d"),
            "adjStkPrc": "1",
        }
        rec = {
            "payload": payload.copy(),
            "payload_value_types": {k: type(v).__name__ for k, v in payload.items()},
            "request_url": KRX_JSON_URL,
        }

        try:
            r = session.post(KRX_JSON_URL, data=payload, headers=h, timeout=30)
            rec["http_status"] = int(r.status_code)
            rec["content_type"] = r.headers.get("Content-Type", "")
            rec["response_length"] = len(r.content or b"")
            rec["response_preview_200"] = (r.text or "").replace("\n", " ").replace("\r", " ")[:200]

            try:
                data = r.json()
                rec["json_parse"] = "OK"
                rec["json_top_keys"] = list(data.keys())[:20] if isinstance(data, dict) else [type(data).__name__]
            except Exception as jex:
                rec["json_parse"] = f"{type(jex).__name__}: {jex}"
                diag["requests"].append(rec)
                cur = chunk_end + timedelta(days=1)
                time.sleep(0.20)
                continue

            rows = data.get("output") or []
            rec["rows"] = int(len(rows))
            if rows:
                x = pd.DataFrame(rows)
                required = {
                    "TRD_DD": "date",
                    "TDD_OPNPRC": "open",
                    "TDD_HGPRC": "high",
                    "TDD_LWPRC": "low",
                    "TDD_CLSPRC": "close",
                    "ACC_TRDVOL": "volume",
                }
                optional = {
                    "ACC_TRDVAL": "trading_value",
                    "MKTCAP": "market_cap",
                    "LIST_SHRS": "listed_shares",
                    "FLUC_TP_CD": "fluc_type_code",
                    "CMPPREVDD_PRC": "change_price",
                    "FLUC_RT": "change_rate_pct",
                }
                missing = [c for c in required if c not in x.columns]
                rec["columns"] = x.columns.tolist()[:40]
                rec["missing_required"] = missing
                rec["optional_present"] = [c for c in optional if c in x.columns]
                if not missing:
                    keep = list(required) + [c for c in optional if c in x.columns]
                    rename_all = dict(required)
                    rename_all.update({c: optional[c] for c in optional if c in x.columns})
                    y = x[keep].rename(columns=rename_all)
                    y["date"] = pd.to_datetime(y["date"], format="%Y/%m/%d", errors="coerce")
                    for c in ["open", "high", "low", "close", "volume",
                              "trading_value", "market_cap", "listed_shares",
                              "change_price", "change_rate_pct", "fluc_type_code"]:
                        if c in y.columns:
                            y[c] = y[c].map(_clean_krx_num)
                    pieces.append(y)
        except Exception as ex:
            rec["request_error"] = f"{type(ex).__name__}: {ex}"

        diag["requests"].append(rec)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.20)

    if not pieces:
        return pd.DataFrame(), diag

    out_df = pd.concat(pieces, ignore_index=True)
    out_df = (
        out_df.dropna(subset=["date", "open", "high", "low", "close", "volume"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    for c in ["open", "high", "low", "close", "volume",
              "trading_value", "market_cap", "listed_shares", "change_price", "fluc_type_code"]:
        if c in out_df.columns:
            out_df[c] = pd.to_numeric(out_df[c], errors="coerce").astype("Int64")
    if "change_rate_pct" in out_df.columns:
        out_df["change_rate_pct"] = pd.to_numeric(out_df["change_rate_pct"], errors="coerce").astype("Float64")
    out_df["source"] = "KRX_DIRECT_RAW_DIAGNOSTIC_ADDENDUM"
    out_df["auto_corrected"] = False
    return out_df, diag


def fetch_krx_direct_raw_isolated(code, start, end, login_id="", login_pw="", retries=2, retry_wait=8.0):
    """
    Batch-only isolation wrapper around the exact verified single-stock fetcher.
    Each attempt enters fetch_krx_direct_raw(), which creates its own fresh
    requests.Session. Failed stock attempts are retried from scratch.
    """
    attempts = []
    last_df = pd.DataFrame()
    last_diag = {}

    for attempt in range(1, int(retries) + 1):
        t0 = time.time()
        df, diag = fetch_krx_direct_raw_diagnostic(
            code, start, end, login_id=login_id, login_pw=login_pw
        )
        attempts.append({
            "attempt": attempt,
            "rows": int(len(df)),
            "elapsed_seconds": round(time.time() - t0, 3),
            "diag": diag,
        })
        last_df, last_diag = df, diag
        if not df.empty:
            return df, {
                "code": code,
                "status": "OK",
                "attempts": attempts,
                "future_outcomes_opened": False,
            }
        if attempt < int(retries):
            time.sleep(float(retry_wait))

    return last_df, {
        "code": code,
        "status": "NO_DATA_AFTER_RETRY",
        "attempts": attempts,
        "last_diag": last_diag,
        "future_outcomes_opened": False,
    }


def parse_universe_bundle(uploaded):
    raw = uploaded.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        names = zf.namelist()

        def pick_exact_or_suffix(exact_name):
            if exact_name in names:
                return exact_name
            hits = [n for n in names if n.endswith("_" + exact_name)]
            if not hits:
                raise ValueError(f"Universe ZIP 필수파일 누락: {exact_name}")
            return sorted(hits)[-1]

        order_name = pick_exact_or_suffix("development_selection_order.csv")
        audit_name = pick_exact_or_suffix("universe_audit.json")
        order = pd.read_csv(zf.open(order_name), dtype={"ticker": str})
        audit = json.load(zf.open(audit_name))

    order["ticker"] = order["ticker"].astype(str).str.zfill(6)
    if "development_selection_order" not in order.columns:
        raise ValueError("development_selection_order 열이 없습니다.")
    order = order.sort_values("development_selection_order").reset_index(drop=True)
    return order, audit


def make_batch_bundle(order_slice, start_date, end_date, login_id="", login_pw="", progress=None):
    results = []
    audit_rows = []
    files = {}

    total = max(1, len(order_slice))
    for i, row in order_slice.iterrows():
        pos = int(row["development_selection_order"])
        ticker = str(row["ticker"]).zfill(6)
        name = str(row.get("name", ticker))
        if progress:
            progress((len(results)) / total, f"{pos}: {name} ({ticker}) 수집 중")

        df, diag = fetch_krx_direct_raw_isolated(
            ticker,
            start_date,
            end_date,
            login_id=login_id,
            login_pw=login_pw,
            retries=2,
            retry_wait=8.0,
        )

        rec = {
            "development_selection_order": pos,
            "ticker": ticker,
            "name": name,
            "market": row.get("market", ""),
            "selection_sector": row.get("selection_sector", ""),
            "selection_stratum": row.get("selection_stratum", ""),
            "status": "OK" if not df.empty else "NO_DATA",
            "rows": int(len(df)),
            "valid_sessions": 0,
            "actual_start": str(df["date"].min().date()) if not df.empty else "",
            "actual_end": str(df["date"].max().date()) if not df.empty else "",
            "trading_value_nonnull": 0,
            "median_trading_value_20d": None,
            "liquidity_status": "",
            "listed_shares_nonnull": 0,
            "market_cap_nonnull": 0,
            "change_rate_nonnull": 0,
            "future_outcomes_opened": False,
        }

        if not df.empty:
            export = df.copy()
            finite_ohlc = export[["open", "high", "low", "close"]].notna().all(axis=1)
            positive_ohlc = (export[["open", "high", "low", "close"]] > 0).all(axis=1)
            export["valid_session"] = (export["volume"].fillna(0) > 0) & finite_ohlc & positive_ohlc
            rec["valid_sessions"] = int(export["valid_session"].sum())

            if "trading_value" in export.columns:
                tv_nonnull = export.loc[export["valid_session"], "trading_value"].notna()
                rec["trading_value_nonnull"] = int(tv_nonnull.sum())
                last20 = export.loc[export["valid_session"] & export["trading_value"].notna(), "trading_value"].tail(20)
                rec["median_trading_value_20d"] = float(last20.median()) if len(last20) == 20 else None
                rec["liquidity_status"] = "OFFICIAL_KRX_ACC_TRDVAL_PRESENT" if rec["trading_value_nonnull"] else "UNKNOWN_NO_TRADING_VALUE"
            else:
                rec["liquidity_status"] = "UNKNOWN_NO_TRADING_VALUE_COLUMN"

            rec["listed_shares_nonnull"] = int(export["listed_shares"].notna().sum()) if "listed_shares" in export.columns else 0
            rec["market_cap_nonnull"] = int(export["market_cap"].notna().sum()) if "market_cap" in export.columns else 0
            rec["change_rate_nonnull"] = int(export["change_rate_pct"].notna().sum()) if "change_rate_pct" in export.columns else 0
            export["date"] = pd.to_datetime(export["date"]).dt.strftime("%Y-%m-%d")
            safe_name = safe_filename_piece(name)
            fn = f"{filename_time_prefix()}_{pos:04d}_{safe_name}_{ticker}_{rec['actual_start'].replace('-','')}_{rec['actual_end'].replace('-','')}.csv"
            payload = export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            files[f"ohlcv/{fn}"] = payload
            rec["sha256"] = hashlib.sha256(payload).hexdigest()
            rec["file"] = f"ohlcv/{fn}"
        else:
            rec["liquidity_status"] = "UNKNOWN_NO_OHLCV"
            rec["sha256"] = ""
            rec["file"] = ""

        audit_rows.append({
            "development_selection_order": pos,
            "ticker": ticker,
            "name": name,
            "diag": diag,
        })
        results.append(rec)
        time.sleep(8.0)  # operational cooling; not a research rule

    manifest_df = pd.DataFrame(results)
    manifest_bytes = manifest_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    files[f"{filename_time_prefix()}_batch_manifest.csv"] = manifest_bytes

    audit_obj = {
        "app_build": "APP_v1.0.18",
        "engine_build": "UNIVERSE_ENGINE_v0.1.14",
        "batch_start_order": int(order_slice["development_selection_order"].min()),
        "batch_end_order": int(order_slice["development_selection_order"].max()),
        "requested_start": str(start_date),
        "requested_end": str(end_date),
        "stock_count": int(len(order_slice)),
        "login_id_present_at_batch_function": bool(login_id),
        "login_pw_present_at_batch_function": bool(login_pw),
        "batch_fetch_path": "fetch_krx_direct_raw_isolated->fetch_krx_direct_raw_diagnostic",
        "future_outcomes_opened": False,
        "stocks": audit_rows,
    }
    audit_bytes = json.dumps(audit_obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    files["batch_audit.json"] = audit_bytes

    sha_manifest = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
    sha_bytes = json.dumps(sha_manifest, ensure_ascii=False, indent=2).encode("utf-8")
    files["SHA256_MANIFEST.json"] = sha_bytes

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)

    if progress:
        progress(1.0, "배치 완료")
    return bio.getvalue(), manifest_df



def fetch_krx_raw(code, start, end):
    """
    KRX 비수정(adjusted=False) OHLCV.
    - 긴 기간은 연도 단위로 분할
    - 각 구간별 성공/빈응답/예외를 diagnostics에 기록
    - adjusted=True 결과는 진단 비교만 수행하며 최종 데이터에는 절대 사용하지 않음
    """
    try:
        import pykrx
        from pykrx import stock
    except Exception as ex:
        raise RuntimeError("pykrx를 불러오지 못했습니다. requirements.txt 설치 상태를 확인하세요.") from ex

    diagnostics = {
        "pykrx_version": getattr(pykrx, "__version__", "unknown"),
        "code": code,
        "requested_start": str(start),
        "requested_end": str(end),
        "chunks": [],
    }

    pieces = []
    cur = start
    while cur <= end:
        chunk_end = min(date(cur.year, 12, 31), end)
        rec = {
            "start": str(cur),
            "end": str(chunk_end),
            "raw_rows": 0,
            "adjusted_probe_rows": None,
            "status": "",
            "error": "",
        }

        try:
            df = stock.get_market_ohlcv_by_date(
                cur.strftime("%Y%m%d"),
                chunk_end.strftime("%Y%m%d"),
                code,
                adjusted=False,
            )
            if df is not None and not df.empty:
                rec["raw_rows"] = int(len(df))
                rec["status"] = "RAW_OK"
                x = df.reset_index()
                x = x.rename(columns={
                    x.columns[0]: "date",
                    "시가": "open",
                    "고가": "high",
                    "저가": "low",
                    "종가": "close",
                    "거래량": "volume",
                })
                pieces.append(x[["date", "open", "high", "low", "close", "volume"]])
            else:
                rec["status"] = "RAW_EMPTY"

                # 진단용 probe: adjusted=True로 같은 기간에 데이터가 존재하는지만 본다.
                # 이 결과는 반환 데이터에 사용하지 않는다.
                try:
                    probe = stock.get_market_ohlcv_by_date(
                        cur.strftime("%Y%m%d"),
                        chunk_end.strftime("%Y%m%d"),
                        code,
                        adjusted=True,
                    )
                    rec["adjusted_probe_rows"] = 0 if probe is None else int(len(probe))
                    if rec["adjusted_probe_rows"] > 0:
                        rec["status"] = "RAW_EMPTY_ADJ_EXISTS"
                except Exception as pex:
                    rec["adjusted_probe_rows"] = -1
                    rec["error"] = f"probe:{type(pex).__name__}: {pex}"

        except Exception as ex:
            rec["status"] = "RAW_ERROR"
            rec["error"] = f"{type(ex).__name__}: {ex}"

        diagnostics["chunks"].append(rec)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.12)

    if not pieces:
        return _standardize(pd.DataFrame(), "KRX_RAW"), diagnostics

    out = _standardize(pd.concat(pieces, ignore_index=True), "KRX_RAW")
    return out, diagnostics


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fdr_candidate(code, start, end):
    """
    FinanceDataReader 장기이력 후보.
    - 5년 단위 분할 요청으로 단일 3,000행 제한 가능성을 회피
    - 조각별 결과를 합친 뒤 날짜 정렬/중복 제거
    - 이 경로는 HRF 연구용 RAW로 자동 승인하지 않음
    """
    try:
        import FinanceDataReader as fdr
    except Exception as ex:
        raise RuntimeError("FinanceDataReader를 불러오지 못했습니다.") from ex

    diag = {
        "fdr_version": getattr(fdr, "__version__", "unknown"),
        "code": code,
        "requested_start": str(start),
        "requested_end": str(end),
        "chunks": [],
    }

    pieces = []
    cur = start
    while cur <= end:
        # 5년 단위로 잘라 요청
        chunk_end = min(date(cur.year + 4, 12, 31), end)
        rec = {
            "start": str(cur),
            "end": str(chunk_end),
            "rows": 0,
            "actual_start": "",
            "actual_end": "",
            "status": "",
            "error": "",
        }
        try:
            df = fdr.DataReader(code, str(cur), str(chunk_end))
            if df is None or df.empty:
                rec["status"] = "FDR_EMPTY"
            else:
                rec["rows"] = int(len(df))
                x = df.reset_index()
                ren = {}
                for c in x.columns:
                    lc = str(c).strip().lower()
                    if lc in ("date", "날짜"): ren[c] = "date"
                    elif lc in ("open", "시가"): ren[c] = "open"
                    elif lc in ("high", "고가"): ren[c] = "high"
                    elif lc in ("low", "저가"): ren[c] = "low"
                    elif lc in ("close", "종가"): ren[c] = "close"
                    elif lc in ("volume", "거래량"): ren[c] = "volume"
                x = x.rename(columns=ren)
                out = _standardize(x, "FDR_CANDIDATE")
                if out.empty:
                    rec["status"] = "FDR_SCHEMA_EMPTY"
                else:
                    rec["status"] = "FDR_OK"
                    rec["actual_start"] = str(out.date.min().date())
                    rec["actual_end"] = str(out.date.max().date())
                    pieces.append(out)
        except Exception as ex:
            rec["status"] = "FDR_ERROR"
            rec["error"] = f"{type(ex).__name__}: {ex}"

        diag["chunks"].append(rec)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.15)

    if not pieces:
        diag["returned_rows"] = 0
        diag["status"] = "FDR_EMPTY_ALL"
        return _standardize(pd.DataFrame(), "FDR_CANDIDATE"), diag

    out = pd.concat(pieces, ignore_index=True)
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    out = _standardize(out, "FDR_CANDIDATE")

    diag["returned_rows"] = int(len(out))
    diag["status"] = "FDR_OK"
    diag["actual_start"] = str(out.date.min().date())
    diag["actual_end"] = str(out.date.max().date())
    return out, diag



INDEX_PRESETS = {
    "KOSPI": {"code": "1001", "name": "KOSPI"},
    "KOSDAQ": {"code": "2001", "name": "KOSDAQ"},
}


def _standardize_index(df, source, index_code, index_name):
    cols = [
        "date", "open", "high", "low", "close", "volume",
        "trading_value", "market_cap", "index_code", "index_name",
        "source", "auto_corrected",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    aliases = {
        "날짜": "date", "시가": "open", "고가": "high", "저가": "low", "종가": "close",
        "거래량": "volume", "거래대금": "trading_value", "상장시가총액": "market_cap",
    }
    out = out.rename(columns=aliases)
    need = ["date", "open", "high", "low", "close", "volume"]
    if not all(c in out.columns for c in need):
        raise ValueError(f"필수 INDEX OHLCV 열 누락: {out.columns.tolist()}")

    if "trading_value" not in out.columns:
        out["trading_value"] = pd.NA
    if "market_cap" not in out.columns:
        out["market_cap"] = pd.NA

    out = out[["date", "open", "high", "low", "close", "volume", "trading_value", "market_cap"]].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "trading_value", "market_cap"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=need).copy()
    # Index price points are decimal; do not coerce OHLC to integer.
    for c in ["open", "high", "low", "close"]:
        out[c] = out[c].astype("float64")
    out["volume"] = out["volume"].astype("int64")
    for c in ["trading_value", "market_cap"]:
        if out[c].notna().all():
            out[c] = out[c].astype("int64")

    out["index_code"] = index_code
    out["index_name"] = index_name
    out["source"] = source
    out["auto_corrected"] = False
    return out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_krx_index(code, name, start, end):
    """
    Market index OHLCV.
    v0.9.9:
    - Streamlit server에서 불안정한 KRX/pykrx index 경로를 사용하지 않는다.
    - FinanceDataReader 지수 심볼을 사용한다:
        KOSPI  -> KS11
        KOSDAQ -> KQ11
    - 3년 단위로 분할 수집 후 날짜 기준 병합/중복 제거.
    - source를 FDR_INDEX로 명시하여 KRX RAW와 혼동하지 않는다.
    - 원자료 자동수정/보간 없음.
    """
    try:
        import FinanceDataReader as fdr
    except Exception as ex:
        raise RuntimeError("FinanceDataReader를 불러오지 못했습니다.") from ex

    symbol_map = {
        "1001": "KS11",
        "2001": "KQ11",
    }
    symbol = symbol_map.get(str(code))
    if symbol is None:
        raise ValueError(f"지원하지 않는 시장지수 코드: {code}")

    diag = {
        "fdr_version": getattr(fdr, "__version__", "unknown"),
        "index_code": str(code),
        "index_name": name,
        "symbol": symbol,
        "requested_start": str(start),
        "requested_end": str(end),
        "chunks": [],
    }

    pieces = []
    cur = start
    while cur <= end:
        # Long-range robustness: <= 3 calendar years per request.
        chunk_end = min(date(cur.year + 2, 12, 31), end)
        rec = {
            "start": str(cur),
            "end": str(chunk_end),
            "rows": 0,
            "actual_start": "",
            "actual_end": "",
            "method": f"FinanceDataReader:{symbol}",
            "status": "",
            "error": "",
        }

        try:
            raw = fdr.DataReader(symbol, str(cur), str(chunk_end))
            if raw is None or raw.empty:
                rec["status"] = "INDEX_EMPTY"
            else:
                x = raw.reset_index()
                ren = {}
                for c in x.columns:
                    lc = str(c).strip().lower()
                    if lc in ("date", "날짜", "index"):
                        ren[c] = "date"
                    elif lc in ("open", "시가"):
                        ren[c] = "open"
                    elif lc in ("high", "고가"):
                        ren[c] = "high"
                    elif lc in ("low", "저가"):
                        ren[c] = "low"
                    elif lc in ("close", "종가"):
                        ren[c] = "close"
                    elif lc in ("volume", "거래량"):
                        ren[c] = "volume"
                x = x.rename(columns=ren)

                required = ["date", "open", "high", "low", "close", "volume"]
                missing = [c for c in required if c not in x.columns]
                if missing:
                    rec["status"] = "INDEX_SCHEMA_ERROR"
                    rec["error"] = f"missing={missing}; columns={x.columns.tolist()}"
                else:
                    # FDR index output normally has no KRX trading_value/market_cap.
                    x["trading_value"] = pd.NA
                    x["market_cap"] = pd.NA
                    out = _standardize_index(
                        x, "FDR_INDEX", str(code), name
                    )

                    if out.empty:
                        rec["status"] = "INDEX_EMPTY_AFTER_STANDARDIZE"
                    else:
                        rec["rows"] = int(len(out))
                        rec["actual_start"] = str(out["date"].min().date())
                        rec["actual_end"] = str(out["date"].max().date())
                        rec["status"] = "INDEX_OK"
                        pieces.append(out)

        except Exception as ex:
            rec["status"] = "INDEX_ERROR"
            rec["error"] = f"{type(ex).__name__}: {ex}"

        diag["chunks"].append(rec)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.15)

    if not pieces:
        diag["returned_rows"] = 0
        diag["status"] = "INDEX_EMPTY_ALL"
        return _standardize_index(
            pd.DataFrame(), "FDR_INDEX", str(code), name
        ), diag

    out = pd.concat(pieces, ignore_index=True)
    out = (
        out.sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    out = _standardize_index(out, "FDR_INDEX", str(code), name)

    diag["returned_rows"] = int(len(out))
    diag["actual_start"] = str(out["date"].min().date())
    diag["actual_end"] = str(out["date"].max().date())
    all_ok = all(r.get("status") == "INDEX_OK" for r in diag["chunks"])
    diag["status"] = "INDEX_OK" if all_ok else "INDEX_PARTIAL"
    return out, diag


def add_index_audit_columns(df):
    out = df.copy()
    hi = out.high < out[["open", "close", "low"]].max(axis=1)
    lo = out.low > out[["open", "close", "high"]].min(axis=1)
    out["ohlc_warning"] = ""
    out.loc[hi, "ohlc_warning"] = "HIGH_LT_MAX_OCL"
    out.loc[lo, "ohlc_warning"] = "LOW_GT_MIN_OCH"
    out.loc[hi & lo, "ohlc_warning"] = "HIGH_AND_LOW_RELATION_ERROR"
    finite_ohlc = out[["open", "high", "low", "close"]].notna().all(axis=1)
    out["valid_session"] = (out["volume"] != 0) & finite_ohlc
    return out


def xmlroot(b):
    t = None
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            t = b.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    t = t if t is not None else b.decode("utf-8", errors="replace")
    t = re.sub(r'^\s*<\?xml[^>]*\?>', '', t, count=1, flags=re.I)
    return ET.fromstring(t)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_naver_fchart(code, start, end):
    # NAVER FCHART는 긴 역사에서 서버측 반환 한계가 있을 수 있으므로
    # 이 함수는 원본 확인/보조용으로 유지하고 coverage를 별도 검사한다.
    count = min(max(max((end - start).days, 1) * 2, 400), 20000)
    r = requests.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"},
        headers=H,
        timeout=30,
    )
    r.raise_for_status()
    rows = []
    for it in xmlroot(r.content).iter("item"):
        p = it.attrib.get("data", "").split("|")
        if len(p) >= 6 and re.fullmatch(r"\d{8}", p[0]):
            rows.append(p[:6])
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    if df.empty:
        return _standardize(df, "NAVER_FCHART")
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    df = df[(df.date.dt.date >= start) & (df.date.dt.date <= end)].copy()
    return _standardize(df, "NAVER_FCHART")


def add_audit_columns(df):
    out = df.copy()
    hi = out.high < out[["open", "close", "low"]].max(axis=1)
    lo = out.low > out[["open", "close", "high"]].min(axis=1)
    out["ohlc_warning"] = ""
    out.loc[hi, "ohlc_warning"] = "HIGH_LT_MAX_OCL"
    out.loc[lo, "ohlc_warning"] = "LOW_GT_MIN_OCH"
    out.loc[hi & lo, "ohlc_warning"] = "HIGH_AND_LOW_RELATION_ERROR"

    # HRF valid_session 규칙을 데이터 자체에 명시적으로 표시한다.
    finite_ohlc = out[["open", "high", "low", "close"]].notna().all(axis=1)
    out["valid_session"] = (out["volume"] != 0) & finite_ohlc
    return out



def filename_time_prefix():
    """Filename sorting prefix requested by user: HHMMSS."""
    return datetime.now().strftime("%H%M%S")


def safe_filename_piece(x):
    x = str(x or "").strip()
    x = re.sub(r'[\\/:*?"<>|]+', "_", x)
    x = re.sub(r"\s+", "_", x)
    return x.strip("._") or "UNKNOWN"


def make_csv_filename(name, df, partial=False):
    created = date.today().strftime("%Y%m%d")
    start = pd.to_datetime(df["date"]).min().strftime("%Y%m%d")
    end = pd.to_datetime(df["date"]).max().strftime("%Y%m%d")
    suffix = "_PARTIAL" if partial else ""
    return f"{filename_time_prefix()}_{safe_filename_piece(name)}_{start}_{end}_생성{created}{suffix}.csv"


# ---------------------------------------------------------------------
# INVESTOR FLOW INPUT — external diagnostic only; HRF CORE is untouched
# ---------------------------------------------------------------------
def _flow_chunk_ranges(start, end, max_days=365):
    if start > end:
        raise ValueError("시작일이 종료일보다 늦습니다.")
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _find_flow_investor_column(columns, investor):
    normalized = {str(column).strip().lower(): column for column in columns}
    for alias in FLOW_INVESTOR_ALIASES[investor]:
        hit = normalized.get(alias.lower())
        if hit is not None:
            return hit
    return None


def normalize_investor_frame(frame, metric, side):
    if metric not in FLOW_METRICS:
        raise ValueError(f"지원하지 않는 metric: {metric}")
    if side not in FLOW_SIDES:
        raise ValueError(f"지원하지 않는 side: {side}")
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date"])

    work = frame.reset_index().copy()
    date_col = next(
        (column for column in work.columns if str(column).strip().lower() in {"날짜", "date"}),
        work.columns[0],
    )
    out = pd.DataFrame({"date": pd.to_datetime(work[date_col], errors="coerce")})
    for investor in FLOW_ALL_INVESTORS:
        source_col = _find_flow_investor_column(work.columns, investor)
        if source_col is not None:
            out[f"{investor}_{side}_{metric}"] = pd.to_numeric(work[source_col], errors="coerce")
    return out.dropna(subset=["date"]).sort_values("date").drop_duplicates("date", keep="last")


def assemble_flow_frames(frames):
    merged = None
    for metric in FLOW_METRICS:
        for side in FLOW_SIDES:
            normalized = normalize_investor_frame(frames.get((metric, side)), metric, side)
            merged = normalized if merged is None else merged.merge(normalized, on="date", how="outer")

    if merged is None or merged.empty:
        return pd.DataFrame()

    expected = [
        f"{investor}_{side}_{metric}"
        for investor in FLOW_ALL_INVESTORS
        for metric in FLOW_METRICS
        for side in FLOW_SIDES
    ]
    for column in expected:
        if column not in merged.columns:
            merged[column] = pd.NA
        merged[column] = pd.to_numeric(merged[column], errors="coerce").round().astype("Int64")

    identity_columns = []
    for investor in FLOW_ALL_INVESTORS:
        for metric in FLOW_METRICS:
            buy = f"{investor}_buy_{metric}"
            sell = f"{investor}_sell_{metric}"
            net = f"{investor}_net_{metric}"
            ok_col = f"{investor}_{metric}_identity_ok"
            complete = merged[[buy, sell, net]].notna().all(axis=1)
            merged[ok_col] = complete & ((merged[buy] - merged[sell]) == merged[net])
            identity_columns.append(ok_col)

    core_columns = [
        f"{investor}_{side}_{metric}"
        for investor in FLOW_CORE_INVESTORS
        for metric in FLOW_METRICS
        for side in FLOW_SIDES
    ]
    merged["flow_input_complete"] = merged[core_columns].notna().all(axis=1)
    merged["flow_identity_ok"] = merged[identity_columns].all(axis=1)

    for investor in FLOW_CORE_INVESTORS:
        buy_qty = pd.to_numeric(merged[f"{investor}_buy_qty"], errors="coerce")
        buy_value = pd.to_numeric(merged[f"{investor}_buy_value"], errors="coerce")
        merged[f"{investor}_gross_buy_avg_price"] = (
            buy_value / buy_qty.where(buy_qty > 0)
        ).astype("Float64")

    merged["flow_source"] = FLOW_SOURCE
    merged["flow_engine_version"] = FLOW_ENGINE_VERSION
    return merged.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def add_flow_finalization_flag(flow, now_kst=None):
    out = flow.copy()
    now = now_kst or datetime.now(ZoneInfo("Asia/Seoul"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Seoul"))
    dates = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["flow_finalized"] = (dates < now.date()) | ((dates == now.date()) & (now.hour >= 18))
    return out


def _flow_diagnostics_template(ticker, start, end, pykrx_version="unknown"):
    return {
        "flow_engine_version": FLOW_ENGINE_VERSION,
        "source": FLOW_SOURCE,
        "pykrx_version": pykrx_version,
        "ticker": str(ticker),
        "requested_start": str(start),
        "requested_end": str(end),
        "auth_attempted": False,
        "auth_success": False,
        "auth_error": "",
        "calls": [],
        "status": "",
    }


def fetch_investor_flow_from_active_session(
    stock_api,
    ticker,
    start,
    end,
    pykrx_version="unknown",
    max_days=365,
    sleep_seconds=0.10,
):
    """Fetch one stock while an authenticated pykrx session is already active."""
    if not re.fullmatch(r"\d{6}", str(ticker)):
        raise ValueError("ticker는 6자리 종목코드여야 합니다.")

    diagnostics = _flow_diagnostics_template(ticker, start, end, pykrx_version)
    diagnostics["auth_attempted"] = True
    diagnostics["auth_success"] = True
    chunk_frames = []
    for chunk_start, chunk_end in _flow_chunk_ranges(start, end, max_days=max_days):
        raw_frames = {}
        for metric in FLOW_METRICS:
            function = (
                stock_api.get_market_trading_volume_by_date
                if metric == "qty"
                else stock_api.get_market_trading_value_by_date
            )
            for side, krx_side in FLOW_SIDES.items():
                record = {
                    "start": str(chunk_start),
                    "end": str(chunk_end),
                    "metric": metric,
                    "side": side,
                    "rows": 0,
                    "status": "",
                    "error": "",
                }
                try:
                    args = (
                        chunk_start.strftime("%Y%m%d"),
                        chunk_end.strftime("%Y%m%d"),
                        str(ticker),
                    )
                    frame = function(*args) if krx_side is None else function(*args, on=krx_side)
                    if frame is None or frame.empty:
                        record["status"] = "EMPTY"
                        raw_frames[(metric, side)] = pd.DataFrame()
                    else:
                        record["rows"] = int(len(frame))
                        probe = normalize_investor_frame(frame, metric, side)
                        required = {
                            f"institution_{side}_{metric}",
                            f"foreign_{side}_{metric}",
                        }
                        if required.issubset(probe.columns):
                            record["status"] = "OK"
                        else:
                            record["status"] = "SCHEMA_ERROR"
                            record["error"] = f"columns={list(map(str, frame.columns))}"
                        raw_frames[(metric, side)] = frame
                except Exception as ex:
                    record["status"] = "ERROR"
                    record["error"] = f"{type(ex).__name__}: {ex}"
                    raw_frames[(metric, side)] = pd.DataFrame()
                diagnostics["calls"].append(record)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        chunk = assemble_flow_frames(raw_frames)
        if not chunk.empty:
            chunk_frames.append(chunk)

    if not chunk_frames:
        diagnostics["status"] = "FLOW_EMPTY"
        return pd.DataFrame(), diagnostics

    out = pd.concat(chunk_frames, ignore_index=True)
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    out = add_flow_finalization_flag(out)
    statuses = [str(record["status"]) for record in diagnostics["calls"]]
    all_calls_ok = bool(statuses) and all(status == "OK" for status in statuses)
    all_rows_complete = bool(out["flow_input_complete"].all())
    all_identities_ok = bool(out["flow_identity_ok"].all())
    diagnostics["status"] = (
        "FLOW_OK" if all_calls_ok and all_rows_complete and all_identities_ok
        else "FLOW_PARTIAL_OR_INVALID"
    )
    diagnostics["rows"] = int(len(out))
    diagnostics["actual_start"] = str(out["date"].min().date())
    diagnostics["actual_end"] = str(out["date"].max().date())
    diagnostics["incomplete_rows"] = int((~out["flow_input_complete"]).sum())
    diagnostics["identity_failure_rows"] = int((~out["flow_identity_ok"]).sum())
    diagnostics["provisional_rows"] = int((~out["flow_finalized"]).sum())
    return out, diagnostics


def fetch_investor_flow_by_date(
    ticker,
    start,
    end,
    login_id="",
    login_pw="",
    max_days=365,
    sleep_seconds=0.10,
):
    if not re.fullmatch(r"\d{6}", str(ticker)):
        raise ValueError("ticker는 6자리 종목코드여야 합니다.")

    diagnostics = _flow_diagnostics_template(ticker, start, end)
    diagnostics["auth_attempted"] = bool(login_id or login_pw)
    if not (login_id and login_pw):
        diagnostics["status"] = "FLOW_AUTH_REQUIRED"
        diagnostics["auth_error"] = "KRX_ID_AND_PASSWORD_REQUIRED"
        return pd.DataFrame(), diagnostics

    try:
        import pykrx
        from pykrx import stock
        from pykrx.website.comm.auth import KRXSession, set_auth_session
    except Exception as ex:
        raise RuntimeError("pykrx를 불러오지 못했습니다. requirements.txt를 확인하세요.") from ex

    pykrx_version = getattr(pykrx, "__version__", "unknown")
    diagnostics["pykrx_version"] = pykrx_version
    auth_session = None
    FLOW_AUTH_LOCK.acquire()
    try:
        auth_session = KRXSession()
        if not auth_session.refresh(login_id, login_pw):
            diagnostics["status"] = "FLOW_AUTH_FAILED"
            diagnostics["auth_error"] = "KRX_LOGIN_REJECTED"
            return pd.DataFrame(), diagnostics
        set_auth_session(auth_session)
        return fetch_investor_flow_from_active_session(
            stock,
            ticker,
            start,
            end,
            pykrx_version=pykrx_version,
            max_days=max_days,
            sleep_seconds=sleep_seconds,
        )
    except Exception as ex:
        diagnostics["status"] = "FLOW_AUTH_OR_SESSION_ERROR"
        diagnostics["auth_error"] = f"{type(ex).__name__}: {ex}"
        return pd.DataFrame(), diagnostics
    finally:
        try:
            set_auth_session(None)
        except Exception:
            pass
        try:
            if auth_session is not None:
                auth_session.session.close()
        except Exception:
            pass
        FLOW_AUTH_LOCK.release()


def _flow_weighted_std(values, weights, mean):
    denominator = float(weights.sum())
    if denominator <= 0:
        return math.nan
    variance = float((weights * (values - mean) ** 2).sum()) / denominator
    return math.sqrt(max(variance, 0.0))


def positive_net_cost_proxy_summary(flow):
    rows = []
    for investor, label in (("institution", "기관합계"), ("foreign", "외국인합계")):
        required = [f"{investor}_buy_qty", f"{investor}_buy_value", f"{investor}_net_qty"]
        if not all(column in flow.columns for column in required):
            continue
        buy_qty = pd.to_numeric(flow[required[0]], errors="coerce")
        buy_value = pd.to_numeric(flow[required[1]], errors="coerce")
        net_qty = pd.to_numeric(flow[required[2]], errors="coerce")
        daily_price = buy_value / buy_qty.where(buy_qty > 0)
        eligible = (
            flow["flow_input_complete"]
            & flow["flow_identity_ok"]
            & (net_qty > 0)
            & daily_price.notna()
        )
        prices = daily_price[eligible].astype(float)
        weights = net_qty[eligible].astype(float)
        total_weight = float(weights.sum())
        proxy = float((prices * weights).sum() / total_weight) if total_weight > 0 else math.nan
        dispersion = _flow_weighted_std(prices, weights, proxy) if total_weight > 0 else math.nan
        rows.append({
            "investor": label,
            "positive_net_days": int(eligible.sum()),
            "positive_net_qty": int(total_weight) if total_weight > 0 else 0,
            "selected_range_net_qty": int(net_qty.fillna(0).sum()),
            "positive_net_addition_price_proxy": proxy,
            "weighted_daily_price_dispersion": dispersion,
        })
    return pd.DataFrame(rows)


def read_ohlcv_upload(uploaded_file):
    raw = uploaded_file.getvalue()
    parsed = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            parsed = pd.read_csv(io.BytesIO(raw), encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if parsed is None:
        raise ValueError("OHLCV CSV 인코딩을 읽지 못했습니다.")
    parsed.columns = [str(column).strip().lower() for column in parsed.columns]
    parsed = parsed.rename(columns={
        "날짜": "date", "시가": "open", "고가": "high", "저가": "low",
        "종가": "close", "거래량": "volume",
    })
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in parsed.columns]
    if missing:
        raise ValueError(f"OHLCV 필수 열 누락: {missing}")
    # Audit 열은 버리지 않고 그대로 보존한 채 필수 열만 형식 검증한다.
    parsed["date"] = pd.to_datetime(parsed["date"], errors="coerce")
    for column in required[1:]:
        parsed[column] = pd.to_numeric(parsed[column], errors="coerce")
    return parsed.dropna(subset=required).sort_values("date").drop_duplicates("date", keep="last")


def merge_ohlcv_and_flow_exact(ohlcv, flow):
    """Merge only when both inputs contain the exact same unique trading dates."""
    left = ohlcv.copy()
    right = flow.copy()
    left["date"] = pd.to_datetime(left["date"], errors="coerce")
    right["date"] = pd.to_datetime(right["date"], errors="coerce")
    diagnostics = {
        "ohlcv_rows": int(len(left)),
        "flow_rows": int(len(right)),
        "common_rows": 0,
        "ohlcv_duplicate_dates": int(left["date"].duplicated().sum()),
        "flow_duplicate_dates": int(right["date"].duplicated().sum()),
        "status": "",
    }
    if left["date"].isna().any() or right["date"].isna().any():
        diagnostics["status"] = "MERGE_INVALID_DATE"
        return pd.DataFrame(), diagnostics
    if diagnostics["ohlcv_duplicate_dates"] or diagnostics["flow_duplicate_dates"]:
        diagnostics["status"] = "MERGE_DUPLICATE_DATE"
        return pd.DataFrame(), diagnostics

    left_dates = set(left["date"])
    right_dates = set(right["date"])
    diagnostics["common_rows"] = int(len(left_dates & right_dates))
    if left_dates != right_dates:
        diagnostics["status"] = "MERGE_DATE_MISMATCH"
        return pd.DataFrame(), diagnostics

    merged = left.merge(right, on="date", how="inner", validate="one_to_one")
    diagnostics["status"] = "MERGE_OK"
    return merged.sort_values("date").reset_index(drop=True), diagnostics


def latest_finalized_flow_date(now_kst=None):
    now = now_kst or datetime.now(ZoneInfo("Asia/Seoul"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Seoul"))
    return now.date() if now.hour >= 18 else now.date() - timedelta(days=1)


def audit_combined_flow_frame(merged):
    """Independent conservation audit; off-session price observations are flagged, not rejected."""
    investors = list(FLOW_ALL_INVESTORS)
    required = ["date", "low", "high", "volume"]
    required += [
        f"{investor}_{side}_{metric}"
        for investor in investors
        for side in FLOW_SIDES
        for metric in FLOW_METRICS
    ]
    missing = [column for column in required if column not in merged.columns]
    out = {
        "rows": int(len(merged)),
        "missing_required_columns": missing,
        "duplicate_dates": int(merged["date"].duplicated().sum()) if "date" in merged else 0,
        "participant_qty_identity_failures": 0,
        "participant_value_identity_failures": 0,
        "buy_qty_vs_volume_failures": 0,
        "sell_qty_vs_volume_failures": 0,
        "net_qty_conservation_failures": 0,
        "buy_value_vs_sell_value_failures": 0,
        "net_value_conservation_failures": 0,
        "institution_buy_avg_outside_regular_ohlc": 0,
        "foreign_buy_avg_outside_regular_ohlc": 0,
        "status": "",
    }
    if missing:
        out["status"] = "CONSERVATION_MISSING_COLUMNS"
        return out

    x = merged.copy()
    for metric in FLOW_METRICS:
        for investor in investors:
            buy = pd.to_numeric(x[f"{investor}_buy_{metric}"], errors="coerce")
            sell = pd.to_numeric(x[f"{investor}_sell_{metric}"], errors="coerce")
            net = pd.to_numeric(x[f"{investor}_net_{metric}"], errors="coerce")
            failures = (buy.isna() | sell.isna() | net.isna() | ((buy - sell) != net))
            key = f"participant_{metric}_identity_failures"
            out[key] += int(failures.sum())

    buy_qty = sum(pd.to_numeric(x[f"{i}_buy_qty"], errors="coerce") for i in investors)
    sell_qty = sum(pd.to_numeric(x[f"{i}_sell_qty"], errors="coerce") for i in investors)
    net_qty = sum(pd.to_numeric(x[f"{i}_net_qty"], errors="coerce") for i in investors)
    buy_value = sum(pd.to_numeric(x[f"{i}_buy_value"], errors="coerce") for i in investors)
    sell_value = sum(pd.to_numeric(x[f"{i}_sell_value"], errors="coerce") for i in investors)
    net_value = sum(pd.to_numeric(x[f"{i}_net_value"], errors="coerce") for i in investors)
    volume = pd.to_numeric(x["volume"], errors="coerce")

    out["buy_qty_vs_volume_failures"] = int((buy_qty.isna() | volume.isna() | (buy_qty != volume)).sum())
    out["sell_qty_vs_volume_failures"] = int((sell_qty.isna() | volume.isna() | (sell_qty != volume)).sum())
    out["net_qty_conservation_failures"] = int((net_qty.isna() | (net_qty != 0)).sum())
    out["buy_value_vs_sell_value_failures"] = int(
        (buy_value.isna() | sell_value.isna() | (buy_value != sell_value)).sum()
    )
    out["net_value_conservation_failures"] = int((net_value.isna() | (net_value != 0)).sum())

    low = pd.to_numeric(x["low"], errors="coerce")
    high = pd.to_numeric(x["high"], errors="coerce")
    for investor in FLOW_CORE_INVESTORS:
        avg = pd.to_numeric(x[f"{investor}_gross_buy_avg_price"], errors="coerce")
        out[f"{investor}_buy_avg_outside_regular_ohlc"] = int(
            (avg.notna() & ((avg < low) | (avg > high))).sum()
        )

    blocking = [
        "duplicate_dates",
        "participant_qty_identity_failures",
        "participant_value_identity_failures",
        "buy_qty_vs_volume_failures",
        "sell_qty_vs_volume_failures",
        "net_qty_conservation_failures",
        "buy_value_vs_sell_value_failures",
        "net_value_conservation_failures",
    ]
    out["status"] = "CONSERVATION_OK" if all(out[key] == 0 for key in blocking) else "CONSERVATION_FAILED"
    return out


def _flow_batch_sorted_order(order_slice):
    required = ["development_selection_order", "ticker"]
    missing = [column for column in required if column not in order_slice.columns]
    if missing:
        raise ValueError(f"Development order 필수 열 누락: {missing}")
    out = order_slice.copy()
    out["ticker"] = out["ticker"].astype(str).str.zfill(6)
    out = out.sort_values("development_selection_order").drop_duplicates("ticker", keep="first")
    if out.empty:
        raise ValueError("수집할 Development 종목이 없습니다.")
    return out.reset_index(drop=True)


def encode_universe_artifacts(universe_full, universe_order, universe_audit):
    """Return auditable Universe inputs for inclusion in the final combined ZIP."""
    full = universe_full.copy()
    order = universe_order.copy()
    for frame in (full, order):
        if "reference_date" in frame.columns:
            frame["reference_date"] = pd.to_datetime(
                frame["reference_date"], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
    return {
        "universe/universe_snapshot.csv": full.to_csv(
            index=False, encoding="utf-8-sig"
        ).encode("utf-8-sig"),
        "universe/development_selection_order.csv": order.to_csv(
            index=False, encoding="utf-8-sig"
        ).encode("utf-8-sig"),
        "universe/universe_audit.json": json.dumps(
            universe_audit or {}, ensure_ascii=False, indent=2, default=str
        ).encode("utf-8"),
    }


def flow_batch_job_id(order_slice, start_date, end_date):
    ordered = _flow_batch_sorted_order(order_slice)
    identity = {
        "flow_batch_engine": FLOW_BATCH_ENGINE_VERSION,
        "flow_engine": FLOW_ENGINE_VERSION,
        "start": str(start_date),
        "end": str(end_date),
        "stocks": [
            [int(row["development_selection_order"]), str(row["ticker"]).zfill(6)]
            for _, row in ordered.iterrows()
        ],
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _atomic_write_json(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    _atomic_write_bytes(path, payload)


def _flow_batch_checkpoint_paths(job_dir, order_number, ticker):
    stem = f"ORD{int(order_number):04d}_{str(ticker).zfill(6)}"
    return Path(job_dir) / f"{stem}.csv", Path(job_dir) / f"{stem}.json"


def load_flow_batch_checkpoint(job_dir, job_id, order_number, ticker, start_date, end_date):
    csv_path, meta_path = _flow_batch_checkpoint_paths(job_dir, order_number, ticker)
    if not csv_path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        payload = csv_path.read_bytes()
    except Exception:
        return None
    expected = {
        "job_id": str(job_id),
        "flow_batch_engine": FLOW_BATCH_ENGINE_VERSION,
        "flow_engine": FLOW_ENGINE_VERSION,
        "development_selection_order": int(order_number),
        "ticker": str(ticker).zfill(6),
        "requested_start": str(start_date),
        "requested_end": str(end_date),
        "status": "OK",
    }
    if any(meta.get(key) != value for key, value in expected.items()):
        return None
    if hashlib.sha256(payload).hexdigest() != meta.get("sha256"):
        return None
    return payload, meta


def save_flow_batch_checkpoint(job_dir, metadata, payload):
    csv_path, meta_path = _flow_batch_checkpoint_paths(
        job_dir,
        metadata["development_selection_order"],
        metadata["ticker"],
    )
    meta = dict(metadata)
    meta["sha256"] = hashlib.sha256(payload).hexdigest()
    _atomic_write_bytes(csv_path, payload)
    _atomic_write_json(meta_path, meta)
    return meta


def fetch_ohlcv_for_flow_batch(ticker, start_date, end_date, retries=2, retry_wait=8.0, fetcher=None):
    fetch = fetcher or fetch_krx_direct_raw
    attempts = []
    last = pd.DataFrame()
    for attempt in range(1, int(retries) + 1):
        t0 = time.time()
        try:
            frame, diagnostics = fetch(ticker, start_date, end_date)
            error = ""
        except Exception as ex:
            frame, diagnostics = pd.DataFrame(), {}
            error = f"{type(ex).__name__}: {ex}"
        attempts.append({
            "attempt": attempt,
            "rows": int(len(frame)),
            "elapsed_seconds": round(time.time() - t0, 3),
            "error": error,
            "diagnostics": diagnostics,
        })
        last = frame
        if not frame.empty:
            return frame, {"status": "OHLCV_OK", "attempts": attempts}
        if attempt < int(retries) and retry_wait > 0:
            time.sleep(float(retry_wait))
    return last, {"status": "OHLCV_EMPTY", "attempts": attempts}


def build_development_flow_batch_with_active_session(
    order_slice,
    start_date,
    end_date,
    stock_api,
    pykrx_version="unknown",
    progress=None,
    checkpoint_root=None,
    ohlcv_fetcher=None,
    flow_max_days=365,
    flow_sleep_seconds=0.25,
    stock_cooldown_seconds=2.0,
    reauthenticate=None,
    universe_full=None,
    universe_order=None,
    universe_audit=None,
):
    ordered = _flow_batch_sorted_order(order_slice)
    job_id = flow_batch_job_id(ordered, start_date, end_date)
    root = Path(checkpoint_root or FLOW_BATCH_CHECKPOINT_ROOT)
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    stock_audits = []
    total = len(ordered)
    for batch_index, row in ordered.iterrows():
        started = time.time()
        position = int(row["development_selection_order"])
        ticker = str(row["ticker"]).zfill(6)
        name = str(row.get("name", ticker))
        if progress:
            progress(batch_index / max(total, 1), f"{batch_index + 1}/{total} · {position}: {name} ({ticker})")

        checkpoint = load_flow_batch_checkpoint(
            job_dir, job_id, position, ticker, start_date, end_date
        )
        if checkpoint is not None:
            _, metadata = checkpoint
            rec = dict(metadata["manifest_record"])
            rec["checkpoint_reused"] = True
            rec["elapsed_seconds"] = round(time.time() - started, 3)
            manifest_rows.append(rec)
            stock_audits.append({
                "development_selection_order": position,
                "ticker": ticker,
                "name": name,
                "checkpoint_reused": True,
                "quality_audit": metadata.get("quality_audit", {}),
            })
            continue

        rec = {
            "development_selection_order": position,
            "ticker": ticker,
            "name": name,
            "market": row.get("market", ""),
            "selection_sector": row.get("selection_sector", ""),
            "selection_stratum": row.get("selection_stratum", ""),
            "status": "",
            "rows": 0,
            "actual_start": "",
            "actual_end": "",
            "valid_sessions": 0,
            "ohlc_warning_rows": 0,
            "flow_status": "",
            "merge_status": "",
            "conservation_status": "",
            "checkpoint_reused": False,
            "elapsed_seconds": 0.0,
            "sha256": "",
            "file": "",
            "error": "",
        }
        stock_audit = {
            "development_selection_order": position,
            "ticker": ticker,
            "name": name,
            "checkpoint_reused": False,
            "ohlcv": {},
            "flow_attempts": [],
            "merge": {},
            "quality_audit": {},
        }

        try:
            ohlcv, ohlcv_diagnostics = fetch_ohlcv_for_flow_batch(
                ticker,
                start_date,
                end_date,
                retries=2,
                retry_wait=8.0 if ohlcv_fetcher is None else 0.0,
                fetcher=ohlcv_fetcher,
            )
            stock_audit["ohlcv"] = ohlcv_diagnostics
            if ohlcv.empty:
                rec["status"] = "OHLCV_EMPTY"
            else:
                ohlcv = add_audit_columns(ohlcv)
                rec["valid_sessions"] = int(ohlcv["valid_session"].sum())
                rec["ohlc_warning_rows"] = int((ohlcv["ohlc_warning"] != "").sum())

                flow = pd.DataFrame()
                flow_diagnostics = {}
                for flow_attempt in range(1, 3):
                    flow, flow_diagnostics = fetch_investor_flow_from_active_session(
                        stock_api,
                        ticker,
                        start_date,
                        end_date,
                        pykrx_version=pykrx_version,
                        max_days=flow_max_days,
                        sleep_seconds=flow_sleep_seconds,
                    )
                    stock_audit["flow_attempts"].append({
                        "attempt": flow_attempt,
                        "diagnostics": flow_diagnostics,
                    })
                    if flow_diagnostics.get("status") == "FLOW_OK":
                        break
                    if flow_attempt == 1 and callable(reauthenticate):
                        stock_audit["reauthentication_attempted"] = True
                        stock_audit["reauthentication_success"] = bool(reauthenticate())
                    if flow_attempt == 1:
                        time.sleep(3.0 if flow_sleep_seconds > 0 else 0.0)

                rec["flow_status"] = flow_diagnostics.get("status", "")
                if flow.empty or rec["flow_status"] != "FLOW_OK":
                    rec["status"] = rec["flow_status"] or "FLOW_EMPTY"
                elif not bool(flow["flow_finalized"].all()):
                    rec["status"] = "FLOW_NOT_FINALIZED"
                else:
                    merged, merge_diagnostics = merge_ohlcv_and_flow_exact(ohlcv, flow)
                    stock_audit["merge"] = merge_diagnostics
                    rec["merge_status"] = merge_diagnostics.get("status", "")
                    if merged.empty or rec["merge_status"] != "MERGE_OK":
                        rec["status"] = rec["merge_status"] or "MERGE_FAILED"
                    else:
                        quality = audit_combined_flow_frame(merged)
                        stock_audit["quality_audit"] = quality
                        rec["conservation_status"] = quality.get("status", "")
                        if rec["conservation_status"] != "CONSERVATION_OK":
                            rec["status"] = rec["conservation_status"] or "CONSERVATION_FAILED"
                        else:
                            export = merged.copy()
                            export["date"] = pd.to_datetime(export["date"]).dt.strftime("%Y-%m-%d")
                            payload = export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                            rec["status"] = "OK"
                            rec["rows"] = int(len(export))
                            rec["actual_start"] = str(export["date"].min())
                            rec["actual_end"] = str(export["date"].max())
                            rec["file"] = (
                                f"combined/ORD{position:04d}_{safe_filename_piece(name)}_{ticker}_"
                                f"{rec['actual_start'].replace('-', '')}_{rec['actual_end'].replace('-', '')}_"
                                "OHLCV_INVESTOR_FLOW.csv"
                            )
                            rec["sha256"] = hashlib.sha256(payload).hexdigest()
                            metadata = {
                                "job_id": job_id,
                                "flow_batch_engine": FLOW_BATCH_ENGINE_VERSION,
                                "flow_engine": FLOW_ENGINE_VERSION,
                                "development_selection_order": position,
                                "ticker": ticker,
                                "requested_start": str(start_date),
                                "requested_end": str(end_date),
                                "status": "OK",
                                "manifest_record": rec,
                                "quality_audit": quality,
                            }
                            save_flow_batch_checkpoint(job_dir, metadata, payload)
        except Exception as ex:
            rec["status"] = "EXCEPTION"
            rec["error"] = f"{type(ex).__name__}: {ex}"

        rec["elapsed_seconds"] = round(time.time() - started, 3)
        manifest_rows.append(rec)
        stock_audits.append(stock_audit)
        if stock_cooldown_seconds > 0:
            time.sleep(float(stock_cooldown_seconds))

    manifest_df = pd.DataFrame(manifest_rows).sort_values("development_selection_order").reset_index(drop=True)
    files = {}
    if universe_full is not None and universe_order is not None:
        files.update(
            encode_universe_artifacts(universe_full, universe_order, universe_audit)
        )
    for _, rec in manifest_df.loc[manifest_df["status"] == "OK"].iterrows():
        checkpoint = load_flow_batch_checkpoint(
            job_dir,
            job_id,
            int(rec["development_selection_order"]),
            str(rec["ticker"]).zfill(6),
            start_date,
            end_date,
        )
        if checkpoint is None:
            continue
        payload, metadata = checkpoint
        files[str(metadata["manifest_record"]["file"])] = payload

    manifest_bytes = manifest_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    files["batch_manifest.csv"] = manifest_bytes
    audit_obj = {
        "app_build": "APP_v1.0.18",
        "flow_batch_engine": FLOW_BATCH_ENGINE_VERSION,
        "flow_engine": FLOW_ENGINE_VERSION,
        "job_id": job_id,
        "requested_start": str(start_date),
        "requested_end": str(end_date),
        "requested_stock_count": int(total),
        "successful_stock_count": int((manifest_df["status"] == "OK").sum()),
        "checkpoint_reused_count": int(manifest_df["checkpoint_reused"].sum()),
        "credentials_stored": False,
        "same_runtime_resume_supported": True,
        "zero_fill_used": False,
        "hrf_core_modified": False,
        "future_outcomes_opened": False,
        "universe_integrated": bool(
            universe_full is not None and universe_order is not None
        ),
        "universe_engine": (universe_audit or {}).get("engine_build", ""),
        "universe_reference_business_day": (universe_audit or {}).get(
            "resolved_reference_business_day", ""
        ),
        "universe_snapshot_rows": int(len(universe_full)) if universe_full is not None else 0,
        "universe_selection_rows": int(len(universe_order)) if universe_order is not None else 0,
        "stocks": stock_audits,
    }
    audit_bytes = json.dumps(audit_obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    files["batch_audit.json"] = audit_bytes
    sha_manifest = {name: hashlib.sha256(payload).hexdigest() for name, payload in files.items()}
    files["SHA256_MANIFEST.json"] = json.dumps(
        sha_manifest, ensure_ascii=False, indent=2
    ).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    if progress:
        progress(1.0, f"완료 {int((manifest_df['status'] == 'OK').sum())}/{total}")
    return output.getvalue(), manifest_df, audit_obj


def make_development_flow_batch_bundle(
    order_slice,
    start_date,
    end_date,
    login_id,
    login_pw,
    progress=None,
    checkpoint_root=None,
    ohlcv_fetcher=None,
    flow_max_days=365,
    flow_sleep_seconds=0.25,
    stock_cooldown_seconds=2.0,
):
    if not (login_id and login_pw):
        raise ValueError("KRX_ID_AND_PASSWORD_REQUIRED")
    try:
        import pykrx
        from pykrx import stock
        from pykrx.website.comm.auth import KRXSession, set_auth_session
    except Exception as ex:
        raise RuntimeError("pykrx를 불러오지 못했습니다. requirements.txt를 확인하세요.") from ex

    pykrx_version = getattr(pykrx, "__version__", "unknown")
    auth_session = None
    FLOW_AUTH_LOCK.acquire()
    try:
        auth_session = KRXSession()
        if not auth_session.refresh(login_id, login_pw):
            raise PermissionError("KRX_LOGIN_REJECTED")
        set_auth_session(auth_session)

        def reauthenticate():
            try:
                ok = bool(auth_session.refresh(login_id, login_pw))
                if ok:
                    set_auth_session(auth_session)
                return ok
            except Exception:
                return False

        def active_session_ohlcv_fetcher(ticker, fetch_start, fetch_end):
            if hasattr(auth_session, "is_valid") and not auth_session.is_valid():
                if not reauthenticate():
                    return pd.DataFrame(), {"status": "KRX_SESSION_REFRESH_FAILED"}
            return fetch_krx_direct_raw_with_active_session(
                auth_session, ticker, fetch_start, fetch_end
            )

        return build_development_flow_batch_with_active_session(
            order_slice,
            start_date,
            end_date,
            stock,
            pykrx_version=pykrx_version,
            progress=progress,
            checkpoint_root=checkpoint_root,
            ohlcv_fetcher=ohlcv_fetcher or active_session_ohlcv_fetcher,
            flow_max_days=flow_max_days,
            flow_sleep_seconds=flow_sleep_seconds,
            stock_cooldown_seconds=stock_cooldown_seconds,
            reauthenticate=reauthenticate,
        )
    finally:
        try:
            set_auth_session(None)
        except Exception:
            pass
        try:
            if auth_session is not None:
                auth_session.session.close()
        except Exception:
            pass
        FLOW_AUTH_LOCK.release()


def make_integrated_universe_flow_batch_bundle(
    reference_date,
    target_count,
    start_date,
    end_date,
    login_id,
    login_pw,
    progress=None,
    checkpoint_root=None,
    ohlcv_fetcher=None,
    flow_max_days=365,
    flow_sleep_seconds=0.25,
    stock_cooldown_seconds=2.0,
):
    """Create the point-in-time Universe and collect the first N stocks in one login."""
    if not (login_id and login_pw):
        raise ValueError("KRX_ID_AND_PASSWORD_REQUIRED")
    target_count = int(target_count)
    if target_count < 1 or target_count > FLOW_BATCH_TARGET_COUNT:
        raise ValueError(f"수집 종목수는 1~{FLOW_BATCH_TARGET_COUNT} 범위여야 합니다.")
    try:
        import pykrx
        from pykrx import stock
        from pykrx.website.comm.auth import KRXSession, set_auth_session
    except Exception as ex:
        raise RuntimeError("pykrx를 불러오지 못했습니다. requirements.txt를 확인하세요.") from ex

    pykrx_version = getattr(pykrx, "__version__", "unknown")
    auth_session = None
    FLOW_AUTH_LOCK.acquire()
    try:
        if progress:
            progress(0.0, "1/2 · KRX 로그인 중")
        auth_session = KRXSession()
        if not auth_session.refresh(login_id, login_pw):
            raise PermissionError("KRX_LOGIN_REJECTED")
        set_auth_session(auth_session)

        if progress:
            progress(0.01, "1/2 · Point-in-Time Universe 생성 중")

        def universe_progress(_, message):
            if progress:
                progress(0.02, f"1/2 · {message}")

        universe_full, universe_order, universe_audit = build_krx_direct_universe(
            auth_session,
            reference_date,
            UniverseConfig(),
            progress=universe_progress,
        )
        if universe_full.empty or universe_order.empty:
            reason = universe_audit.get("snapshot_failure", "usable selection order 없음")
            raise RuntimeError(f"UNIVERSE_GENERATION_FAILED: {reason}")
        if len(universe_order) < target_count:
            raise RuntimeError(
                f"UNIVERSE_TOO_SMALL: selection order {len(universe_order)} < requested {target_count}"
            )
        selection = _flow_batch_sorted_order(universe_order).head(target_count).copy()
        if progress:
            progress(
                0.04,
                f"1/2 · Universe 완료 {len(universe_full):,}종목 · 수집대상 {len(selection)}종목",
            )

        def reauthenticate():
            try:
                ok = bool(auth_session.refresh(login_id, login_pw))
                if ok:
                    set_auth_session(auth_session)
                return ok
            except Exception:
                return False

        def active_session_ohlcv_fetcher(ticker, fetch_start, fetch_end):
            if hasattr(auth_session, "is_valid") and not auth_session.is_valid():
                if not reauthenticate():
                    return pd.DataFrame(), {"status": "KRX_SESSION_REFRESH_FAILED"}
            return fetch_krx_direct_raw_with_active_session(
                auth_session, ticker, fetch_start, fetch_end
            )

        def batch_progress(value, message):
            if progress:
                progress(0.04 + 0.96 * min(max(float(value), 0.0), 1.0), f"2/2 · {message}")

        payload, manifest, batch_audit = build_development_flow_batch_with_active_session(
            selection,
            start_date,
            end_date,
            stock,
            pykrx_version=pykrx_version,
            progress=batch_progress,
            checkpoint_root=checkpoint_root,
            ohlcv_fetcher=ohlcv_fetcher or active_session_ohlcv_fetcher,
            flow_max_days=flow_max_days,
            flow_sleep_seconds=flow_sleep_seconds,
            stock_cooldown_seconds=stock_cooldown_seconds,
            reauthenticate=reauthenticate,
            universe_full=universe_full,
            universe_order=universe_order,
            universe_audit=universe_audit,
        )
        return (
            payload,
            manifest,
            batch_audit,
            universe_full,
            universe_order,
            universe_audit,
        )
    finally:
        try:
            set_auth_session(None)
        except Exception:
            pass
        try:
            if auth_session is not None:
                auth_session.session.close()
        except Exception:
            pass
        FLOW_AUTH_LOCK.release()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_flow_cached(ticker, start, end):
    return fetch_investor_flow_by_date(ticker, start, end)


st.title("Korea OHLCV CSV v1.0.18 UNIVERSE + OHLCV + FLOW 99")
st.caption(
    "개별주식 KRX DIRECT RAW + KOSPI/KOSDAQ 지수(FDR) + "
    "Track 02 Development Universe · 99종목 일괄 수집 · 원본 보존 · outcome-blind"
)

st.caption(
    "BUILD: APP_v1.0.18 / UNIVERSE_DIRECT_v0.1.0 / "
    "BATCH_OHLCV_v0.6 / INVESTOR_FLOW_INPUT_v0.3.0 / FLOW_BATCH_v0.2.0"
)

data_kind = st.radio(
    "수집 대상",
    [
        "개별주식",
        "시장지수",
        "Development Universe",
        "Development Batch OHLCV",
        "Development Universe + OHLCV + FLOW (99)",
    ],
    horizontal=True,
)

# ---------------------------------------------------------------------
# INVESTOR FLOW
# ---------------------------------------------------------------------
if data_kind == "기관·외국인 수급":
    st.info(
        "기관합계·외국인합계의 일별 매수/매도/순매수 수량과 금액을 수집합니다. "
        "이 기능은 HRF Living Map v1.0 CORE와 분리된 외부 진단 입력이며 S1/NEXT를 변경하지 않습니다."
    )
    flow_query = st.text_input(
        "종목명 또는 6자리 종목코드",
        value="삼성전자",
        key="flow_query",
    )
    flow_left, flow_right = st.columns(2)
    with flow_left:
        flow_start = st.date_input(
            "수급 시작일",
            value=date(2021, 1, 4),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
            key="flow_start",
        )
    with flow_right:
        flow_end = st.date_input(
            "수급 종료일",
            value=date.today(),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
            key="flow_end",
        )

    flow_ohlcv_upload = st.file_uploader(
        "기존 앱에서 받은 OHLCV CSV 연결",
        type=["csv"],
        help="삼성전자 OHLCV CSV를 먼저 선택하면 수급과 날짜별로 결합한 연구입력 CSV까지 만듭니다.",
        key="flow_ohlcv_upload",
    )
    st.warning(
        "원가 후보는 실제 기관·외국인 보유잔고 원가가 아닙니다. "
        "양(+) 순매수일의 일별 매수금액÷매수수량을 양(+) 순매수수량으로 가중한 연구용 후보치입니다."
    )

    if st.button("기관·외국인 수급 + 결합 CSV 만들기", type="primary", use_container_width=True):
        if flow_start > flow_end:
            st.error("수급 시작일과 종료일을 확인하세요.")
            st.stop()

        flow_security = resolve(flow_query)
        if not flow_security:
            st.error("종목을 찾지 못했습니다. 종목명 또는 6자리 종목코드를 확인해 주세요.")
            st.stop()

        try:
            with st.spinner("KRX 투자자별 수량·금액을 구간별로 수집하고 검산하는 중입니다."):
                flow, flow_diagnostics = fetch_flow_cached(
                    flow_security["code"], flow_start, flow_end
                )
        except Exception as ex:
            st.error(f"수급 수집 실패: {type(ex).__name__}: {ex}")
            st.stop()

        st.write(f"종목: **{flow_security['name']} ({flow_security['code']})**")
        st.write(
            f"상태: **{flow_diagnostics.get('status', '')}** · "
            f"pykrx: **{flow_diagnostics.get('pykrx_version', 'unknown')}**"
        )
        flow_calls = pd.DataFrame(flow_diagnostics.get("calls", []))
        if not flow_calls.empty:
            with st.expander(
                "KRX 수급 분할수집 로그",
                expanded=flow_diagnostics.get("status") != "FLOW_OK",
            ):
                st.dataframe(flow_calls, use_container_width=True, hide_index=True)

        if flow.empty:
            st.error("수급 응답이 비었습니다. 빈 응답을 0으로 대체하지 않았습니다.")
            st.stop()

        flow_ok = flow_diagnostics.get("status") == "FLOW_OK"
        if flow_ok:
            st.success(f"{len(flow):,}개 거래일 수급 수집 및 항등식 검산 통과")
        else:
            st.error(
                "일부 호출·스키마 또는 매수-매도=순매수 검산에 실패했습니다. "
                "이 출력은 완성 연구 입력으로 사용하지 마세요."
            )

        provisional_rows = int((~flow["flow_finalized"]).sum())
        if provisional_rows:
            st.warning(
                f"오후 6시 이전 당일 수급 {provisional_rows}행은 provisional입니다. "
                "종가 연구에는 18:00 KST 이후 다시 받으세요."
            )

        flow_summary = positive_net_cost_proxy_summary(flow)
        if not flow_summary.empty:
            flow_summary_view = flow_summary.copy()
            for column in ("positive_net_addition_price_proxy", "weighted_daily_price_dispersion"):
                flow_summary_view[column] = flow_summary_view[column].round(2)
            st.markdown("#### 선택 기간 양(+) 순매수 원가 후보")
            st.dataframe(flow_summary_view, use_container_width=True, hide_index=True)

        flow_preview_columns = [
            "date",
            "institution_net_qty",
            "institution_net_value",
            "institution_gross_buy_avg_price",
            "foreign_net_qty",
            "foreign_net_value",
            "foreign_gross_buy_avg_price",
            "flow_input_complete",
            "flow_identity_ok",
            "flow_finalized",
        ]
        st.markdown("#### 최근 수급")
        st.dataframe(flow[flow_preview_columns].tail(30), use_container_width=True, hide_index=True)

        flow_export = flow.copy()
        flow_export["date"] = pd.to_datetime(flow_export["date"]).dt.strftime("%Y-%m-%d")
        flow_payload = flow_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        flow_suffix = "" if flow_ok and provisional_rows == 0 else "_NOT_RESEARCH_READY"
        st.download_button(
            "수급 원자료 CSV 다운로드",
            flow_payload,
            file_name=(
                f"{flow_security['code']}_{flow_start:%Y%m%d}_{flow_end:%Y%m%d}"
                f"_investor_flow{flow_suffix}.csv"
            ),
            mime="text/csv",
            use_container_width=True,
        )

        if flow_ohlcv_upload is None:
            st.info("결합 CSV가 필요하면 위에서 삼성전자 OHLCV CSV를 선택한 뒤 버튼을 다시 누르세요.")
        elif not flow_ok or provisional_rows:
            st.error("수급 데이터가 완전 확정되지 않아 OHLCV 결합 연구파일 생성을 차단했습니다.")
        else:
            try:
                flow_ohlcv = read_ohlcv_upload(flow_ohlcv_upload)
                merged_flow = flow_ohlcv.merge(
                    flow,
                    on="date",
                    how="inner",
                    validate="one_to_one",
                )
                ohlcv_dates = set(flow_ohlcv["date"])
                flow_dates = set(pd.to_datetime(flow["date"]))
                exact_date_match = ohlcv_dates == flow_dates
                if merged_flow.empty:
                    st.error("OHLCV와 수급의 공통 날짜가 없습니다.")
                elif not exact_date_match:
                    st.error(
                        f"날짜 불일치: OHLCV {len(flow_ohlcv):,}행 · 수급 {len(flow):,}행 · "
                        f"공통 {len(merged_flow):,}행. 결합 파일 생성을 차단했습니다."
                    )
                else:
                    merged_export = merged_flow.copy()
                    merged_export["date"] = merged_export["date"].dt.strftime("%Y-%m-%d")
                    merged_payload = merged_export.to_csv(
                        index=False, encoding="utf-8-sig"
                    ).encode("utf-8-sig")
                    st.success(
                        f"OHLCV와 수급의 날짜가 전부 일치했습니다: **{len(merged_flow):,}거래일**"
                    )
                    st.download_button(
                        "OHLCV + 수급 연구입력 CSV 다운로드",
                        merged_payload,
                        file_name=(
                            f"{flow_security['code']}_{flow_start:%Y%m%d}_{flow_end:%Y%m%d}"
                            "_ohlcv_investor_flow.csv"
                        ),
                        mime="text/csv",
                        use_container_width=True,
                    )
            except Exception as ex:
                st.error(f"OHLCV 병합 실패: {type(ex).__name__}: {ex}")

    st.divider()
    st.caption(
        "연구 경계: 단순 가격 평균과 순매수금액÷순매수수량은 실제 원가로 사용하지 않습니다. "
        "수급 Episode 규칙과 OOS 검증 전에는 매매 신호로 승격하지 않습니다."
    )

# ---------------------------------------------------------------------
# DEVELOPMENT UNIVERSE
# ---------------------------------------------------------------------
elif data_kind == "Development Universe":
    st.info(
        "Track 02용 point-in-time 후보군을 만듭니다. "
        "H15/MFE/MAE 등 미래 outcome은 사용하지 않습니다."
    )
    ref = st.date_input(
        "Universe 기준일",
        value=date.today(),
        min_value=date(2000, 1, 1),
        max_value=date.today(),
        key="universe_ref_date",
    )
    st.caption(
        "고정 설계: KOSPI+KOSDAQ · Industry 기반 broad sector · 시장별 시총 tercile · "
        "직전 20거래일 중위 거래대금 · ticker-stable round-robin"
    )

    if st.button("Point-in-Time Universe 만들기", type="primary", use_container_width=True):
        prog = st.progress(0, text="Universe 준비 중")

        def cb(p, msg):
            prog.progress(min(max(float(p), 0.0), 1.0), text=msg)

        try:
            full, ordered, diag = build_universe(ref, UniverseConfig(), progress=cb)
        except Exception as ex:
            prog.empty()
            st.error(f"Universe 생성 실패: {type(ex).__name__}: {ex}")
            st.stop()

        prog.empty()
        if full.empty:
            st.error("Universe snapshot을 가져오지 못했습니다.")
            st.write(
                f"실행 엔진: **{diag.get('engine_build', 'UNKNOWN/OLD ENGINE')}** / "
                f"snapshot source: **{diag.get('snapshot_source', 'NONE')}**"
            )
            st.json(diag)
            st.stop()

        st.success(
            f"기준 영업일 {diag.get('resolved_reference_business_day')} · "
            f"snapshot {len(full):,}종목 · common-stock candidate "
            f"{int(full['eligible_common_candidate'].sum()):,}종목"
        )

        if diag.get("errors"):
            st.warning(
                f"수집 오류 {len(diag['errors'])}건. audit JSON을 확인하세요."
            )
            with st.expander("Universe 오류 로그"):
                st.dataframe(pd.DataFrame(diag["errors"]), use_container_width=True, hide_index=True)

        st.info(
            "20거래일 중위 거래대금은 전종목 pykrx 경로 오류 때문에 여기서 만들지 않습니다. "
            "선정 순서에는 유동성을 사용하지 않고, 각 후보의 검증 OHLCV를 수집할 때 "
            "직전 20거래일 중위 거래대금을 계산한 뒤 Development eligibility를 판정합니다."
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("전체 snapshot", f"{len(full):,}")
        c2.metric("Eligible", f"{int(full['eligible_common_candidate'].sum()):,}")
        c3.metric(
            "KOSPI eligible",
            f"{int(((full.market=='KOSPI') & full.eligible_common_candidate).sum()):,}",
        )
        c4.metric(
            "KOSDAQ eligible",
            f"{int(((full.market=='KOSDAQ') & full.eligible_common_candidate).sum()):,}",
        )

        st.subheader("Deterministic Development Selection Order")
        show_cols = [
            "development_selection_order", "ticker", "name", "market", "selection_sector", "industry",
            "market_cap", "median_trading_value_20d", "market_cap_bucket",
            "liquidity_bucket", "selection_stratum", "classification_review_flag",
        ]
        st.dataframe(ordered[show_cols].head(200), use_container_width=True, hide_index=True)
        st.caption("화면에는 앞 200개만 표시하고, 다운로드 ZIP에는 전체 순서를 저장합니다.")

        full_out = full.copy()
        ordered_out = ordered.copy()
        if "reference_date" in full_out:
            full_out["reference_date"] = pd.to_datetime(full_out["reference_date"]).dt.strftime("%Y-%m-%d")
        if "reference_date" in ordered_out:
            ordered_out["reference_date"] = pd.to_datetime(ordered_out["reference_date"]).dt.strftime("%Y-%m-%d")

        full_bytes = full_out.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        order_bytes = ordered_out.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        audit_bytes = json.dumps(diag, ensure_ascii=False, indent=2, default=str).encode("utf-8")

        csv_prefix = filename_time_prefix()
        universe_csv_name = f"{csv_prefix}_universe_snapshot.csv"
        order_csv_name = f"{csv_prefix}_development_selection_order.csv"

        manifest = {
            universe_csv_name: hashlib.sha256(full_bytes).hexdigest(),
            order_csv_name: hashlib.sha256(order_bytes).hexdigest(),
            "universe_audit.json": hashlib.sha256(audit_bytes).hexdigest(),
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(universe_csv_name, full_bytes)
            zf.writestr(order_csv_name, order_bytes)
            zf.writestr("universe_audit.json", audit_bytes)
            zf.writestr("SHA256_MANIFEST.json", manifest_bytes)

        resolved = diag.get("resolved_reference_business_day", ref.strftime("%Y%m%d"))
        created = date.today().strftime("%Y%m%d")
        universe_filename = f"UNIVERSE_기준{resolved}_생성{created}.zip"

        st.download_button(
            "Universe Audit Bundle ZIP 다운로드",
            bio.getvalue(),
            file_name=universe_filename,
            mime="application/zip",
            use_container_width=True,
        )
        st.caption(
            "Safari 다운로드 위치를 '나의 iPhone/GPT/주식시세추이'로 지정했다면 "
            "위 ZIP은 그 폴더로 저장됩니다."
        )

# ---------------------------------------------------------------------
# DEVELOPMENT BATCH OHLCV
# ---------------------------------------------------------------------
elif data_kind == "Development Batch OHLCV":
    st.success(
        "Track02 Data Addendum build: 기존 OHLCV에 KRX 공식 거래대금(ACC_TRDVAL), "
        "시가총액(MKTCAP), 상장주식수(LIST_SHRS), KRX 등락률을 추가 보존합니다. "
        "미래 response outcome은 계산하지 않습니다."
    )
    st.info(
        "승인된 Universe Audit Bundle ZIP을 입력으로 사용해 deterministic selection order를 그대로 따라갑니다. "
        "배치 크기는 서버 운영 단위일 뿐 연구 표본수 기준이 아닙니다. 첫 재시험은 2종목입니다."
    )

    uploaded = st.file_uploader(
        "승인된 Universe Audit Bundle ZIP",
        type=["zip"],
        accept_multiple_files=False,
    )

    c1, c2 = st.columns(2)
    with c1:
        batch_size = st.number_input(
            "운영 배치 크기",
            min_value=1,
            max_value=20,
            value=2,
            step=1,
            help="첫 검증은 2종목. 성공하면 5→10으로 확대. 연구 표본수 기준이 아닙니다.",
        )
    with c2:
        batch_no = st.number_input(
            "배치 번호",
            min_value=1,
            value=1,
            step=1,
        )

    d1, d2 = st.columns(2)
    with d1:
        batch_start = st.date_input(
            "OHLCV 시작일",
            value=date(2010, 1, 4),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
            key="batch_start",
        )
    with d2:
        batch_end = st.date_input(
            "OHLCV 종료일",
            value=date.today(),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
            key="batch_end",
        )

    st.info(
        "Batch KRX 수집은 현재 인증 세션이 필요합니다. "
        "ID/PW는 아래 실행 폼에서 입력하고 같은 폼의 실행 버튼을 누릅니다. "
        "값 자체는 ZIP/audit에 저장하지 않습니다."
    )

    if uploaded is not None:
        try:
            order, uaudit = parse_universe_bundle(uploaded)
        except Exception as ex:
            st.error(f"Universe ZIP 해석 실패: {type(ex).__name__}: {ex}")
            st.stop()

        st.write(
            f"Universe engine: **{uaudit.get('engine_build', 'UNKNOWN')}** · "
            f"candidate rows: **{len(order):,}** · "
            f"outcomes opened: **{uaudit.get('outcomes_opened', 'UNKNOWN')}**"
        )

        start_idx = (int(batch_no) - 1) * int(batch_size)
        end_idx = min(start_idx + int(batch_size), len(order))
        if start_idx >= len(order):
            st.warning("해당 배치 번호는 selection order 범위를 벗어납니다.")
        else:
            sl = order.iloc[start_idx:end_idx].copy()
            st.subheader(
                f"Batch {int(batch_no)} · selection order "
                f"{int(sl.development_selection_order.min())}~{int(sl.development_selection_order.max())}"
            )
            show = [c for c in [
                "development_selection_order", "ticker", "name", "market",
                "selection_sector", "market_cap_bucket", "selection_stratum"
            ] if c in sl.columns]
            st.dataframe(sl[show], use_container_width=True, hide_index=True)

            with st.form("development_batch_run_form", clear_on_submit=False):
                st.caption(
                    "KRX 로그인값과 실행 버튼을 같은 form에 넣어 Streamlit rerun으로 "
                    "ID/PW가 사라지는 문제를 막습니다."
                )
                batch_krx_id = st.text_input("KRX ID", value="", key="batch_krx_id_form")
                batch_krx_pw = st.text_input(
                    "KRX 비밀번호", value="", type="password", key="batch_krx_pw_form"
                )
                st.caption("ID/PW 입력 여부만 검사하며 실제 값은 audit에 기록하지 않습니다.")
                run_batch = st.form_submit_button(
                    "Development Batch OHLCV 만들기",
                    type="primary",
                    use_container_width=True,
                )

            if run_batch:
                if batch_start > batch_end:
                    st.error("OHLCV 시작일/종료일을 확인하세요.")
                    st.stop()

                if not batch_krx_id or not batch_krx_pw:
                    st.error(
                        "KRX ID와 비밀번호가 Batch 함수에 전달되기 전에 비어 있습니다. "
                        "두 칸 모두 입력한 뒤 같은 폼의 실행 버튼을 눌러주세요."
                    )
                    st.stop()

                # Safe UI confirmation: never expose credential values or lengths.
                st.success("KRX ID/PW 입력 감지: YES / YES")

                prog = st.progress(0, text="배치 준비 중")
                def pcb(p, msg):
                    prog.progress(min(max(float(p), 0.0), 1.0), text=msg)

                try:
                    payload, manifest_df = make_batch_bundle(
                        sl,
                        batch_start,
                        batch_end,
                        login_id=batch_krx_id,
                        login_pw=batch_krx_pw,
                        progress=pcb,
                    )
                except Exception as ex:
                    prog.empty()
                    st.error(f"Batch 생성 실패: {type(ex).__name__}: {ex}")
                    st.stop()
                prog.empty()

                ok = int((manifest_df["status"] == "OK").sum())
                liq_ready = int((manifest_df["liquidity_status"] == "READY_20_VALID_SESSIONS").sum())
                st.success(
                    f"배치 완료 · OHLCV 성공 {ok}/{len(manifest_df)} · "
                    f"20일 중위 거래대금 준비 {liq_ready}/{len(manifest_df)}"
                )
                st.dataframe(manifest_df, use_container_width=True, hide_index=True)

                created = date.today().strftime("%Y%m%d")
                bname = (
                    f"DEV_BATCH_{int(batch_no):03d}_"
                    f"ORD{int(sl.development_selection_order.min()):04d}-"
                    f"{int(sl.development_selection_order.max()):04d}_"
                    f"{batch_start.strftime('%Y%m%d')}_{batch_end.strftime('%Y%m%d')}_"
                    f"생성{created}.zip"
                )
                st.download_button(
                    "Development Batch ZIP 다운로드",
                    payload,
                    file_name=bname,
                    mime="application/zip",
                    use_container_width=True,
                )
                st.caption(
                    "ZIP 안에는 종목별 OHLCV CSV, batch_manifest.csv, batch_audit.json, "
                    "SHA256_MANIFEST.json이 들어 있습니다. H15/MFE/MAE는 생성하지 않습니다."
                )

# ---------------------------------------------------------------------
# DEVELOPMENT UNIVERSE + OHLCV + INVESTOR FLOW — one-click, sequential, resumable
# ---------------------------------------------------------------------
elif data_kind == "Development Universe + OHLCV + FLOW (99)":
    st.success(
        "Universe 생성과 앞 99종목 OHLCV·기관/외국인 수급 수집을 한 번에 실행합니다. "
        "중간 Universe ZIP 다운로드·재업로드 단계는 없습니다."
    )
    st.warning(
        "99종목은 KRX 요청량이 많아 수 시간이 걸릴 수 있습니다. 병렬 폭주는 사용하지 않습니다. "
        "연결이 끊기면 같은 기준일·수집기간·종목수로 다시 실행하세요. 같은 실행 서버에 남은 "
        "완료 체크포인트는 건너뛰지만 앱 재배포·서버 재시작 시 체크포인트가 사라질 수 있습니다."
    )
    st.info(
        "기준일 스냅샷은 로그인된 KRX의 MDCSTAT01501·03901을 직접 사용합니다. "
        "pykrx의 '종가/시가총액' 열 오류를 우회하되, 현재 종목표를 과거 기준일에 섞지 않습니다. "
        "HRF CORE·S1·NEXT는 변경하지 않습니다."
    )

    fb1, fb2, fb3 = st.columns(3)
    with fb1:
        integrated_universe_ref = st.date_input(
            "Universe 기준일",
            value=latest_finalized_flow_date(),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
            key="integrated_universe_ref",
            help="휴장일이면 이 날짜 이전의 가장 가까운 KRX 거래일로 자동 확정합니다.",
        )
    with fb2:
        flow_batch_count = st.number_input(
            "수집 종목수",
            min_value=1,
            max_value=FLOW_BATCH_TARGET_COUNT,
            value=FLOW_BATCH_TARGET_COUNT,
            step=1,
            help="기본값 99. 운영 점검 때만 줄이고 본 수집은 99로 실행합니다.",
        )
    with fb3:
        st.metric("최종 확정 수급 가능일", str(latest_finalized_flow_date()))

    fbd1, fbd2 = st.columns(2)
    with fbd1:
        flow_batch_start = st.date_input(
            "결합 시작일",
            value=date(2021, 1, 4),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
            key="flow_batch_start",
        )
    with fbd2:
        flow_batch_end = st.date_input(
            "결합 종료일",
            value=latest_finalized_flow_date(),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
            key="flow_batch_end",
        )

    target_count = int(flow_batch_count)
    with st.form("integrated_universe_flow_batch_form", clear_on_submit=False):
        st.caption(
            "KRX 로그인은 Universe 생성부터 수급 수집까지 한 실행에서 사용합니다. "
            "ID/PW 값은 CSV·ZIP·audit·체크포인트에 저장하지 않습니다."
        )
        flow_batch_krx_id = st.text_input(
            "KRX ID", value="", key="integrated_flow_batch_krx_id_form"
        )
        flow_batch_krx_pw = st.text_input(
            "KRX 비밀번호", value="", type="password", key="integrated_flow_batch_krx_pw_form"
        )
        run_flow_batch = st.form_submit_button(
            f"Universe 생성 + {target_count}종목 OHLCV·FLOW 일괄 만들기",
            type="primary",
            use_container_width=True,
        )

    if run_flow_batch:
        if flow_batch_start > flow_batch_end:
            st.error("결합 시작일/종료일을 확인하세요.")
            st.stop()
        if flow_batch_end > latest_finalized_flow_date():
            st.error(
                f"수급 확정 전 날짜가 포함됐습니다. 종료일을 "
                f"{latest_finalized_flow_date()} 이하로 설정하세요."
            )
            st.stop()
        if not flow_batch_krx_id or not flow_batch_krx_pw:
            st.error("KRX ID와 비밀번호를 모두 입력한 뒤 같은 폼의 실행 버튼을 눌러주세요.")
            st.stop()

        st.success("KRX ID/PW 입력 감지: YES / YES · 값은 저장하지 않습니다.")
        flow_progress = st.progress(0, text="Universe + 99종목 수집 준비 중")

        def flow_batch_progress(value, message):
            flow_progress.progress(min(max(float(value), 0.0), 1.0), text=message)

        try:
            (
                flow_payload,
                flow_manifest,
                flow_batch_audit,
                flow_universe_full,
                flow_order,
                flow_universe_audit,
            ) = make_integrated_universe_flow_batch_bundle(
                integrated_universe_ref,
                target_count,
                flow_batch_start,
                flow_batch_end,
                login_id=flow_batch_krx_id,
                login_pw=flow_batch_krx_pw,
                progress=flow_batch_progress,
            )
        except PermissionError:
            flow_progress.empty()
            st.error("KRX 로그인이 거부됐습니다. ID·비밀번호 또는 비밀번호 변경 필요 여부를 확인하세요.")
            st.stop()
        except Exception as ex:
            flow_progress.empty()
            st.error(f"Universe + 99종목 결합 실패: {type(ex).__name__}: {ex}")
            st.caption("같은 기준일·수집기간·종목수로 다시 실행하면 완료 체크포인트부터 재개합니다.")
            st.stop()
        flow_progress.empty()

        flow_slice = _flow_batch_sorted_order(flow_order).head(target_count).copy()
        flow_job_id = flow_batch_job_id(flow_slice, flow_batch_start, flow_batch_end)
        st.write(
            f"Universe 기준 영업일: **{flow_universe_audit.get('resolved_reference_business_day', 'UNKNOWN')}** · "
            f"snapshot: **{len(flow_universe_full):,}종목** · "
            f"selection order: **{len(flow_order):,}종목** · 수집 대상: **{len(flow_slice)}종목** · "
            f"job: **{flow_job_id}**"
        )
        if flow_universe_audit.get("errors"):
            with st.expander("Universe 보조 분류 오류 로그"):
                st.dataframe(
                    pd.DataFrame(flow_universe_audit["errors"]),
                    use_container_width=True,
                    hide_index=True,
                )
        show_columns = [column for column in [
            "development_selection_order", "ticker", "name", "market",
            "selection_sector", "market_cap_bucket", "selection_stratum",
        ] if column in flow_slice.columns]
        st.dataframe(flow_slice[show_columns], use_container_width=True, hide_index=True)

        flow_ok_count = int((flow_manifest["status"] == "OK").sum())
        flow_reused_count = int(flow_manifest["checkpoint_reused"].sum())
        if flow_ok_count == len(flow_manifest):
            st.success(
                f"Universe + {len(flow_manifest)}종목 결합 완료 · "
                f"성공 {flow_ok_count}/{len(flow_manifest)} · 체크포인트 재사용 {flow_reused_count}"
            )
        else:
            st.warning(
                f"부분 완료 · 성공 {flow_ok_count}/{len(flow_manifest)} · "
                f"체크포인트 재사용 {flow_reused_count}. 실패 종목은 0으로 채우지 않았습니다. "
                "같은 설정으로 다시 실행하세요."
            )

        manifest_view_columns = [column for column in [
            "development_selection_order", "ticker", "name", "status", "rows",
            "actual_start", "actual_end", "flow_status", "merge_status",
            "conservation_status", "checkpoint_reused", "elapsed_seconds", "error",
        ] if column in flow_manifest.columns]
        st.dataframe(
            flow_manifest[manifest_view_columns],
            use_container_width=True,
            hide_index=True,
        )

        partial_suffix = "" if flow_ok_count == len(flow_manifest) else "_PARTIAL"
        created = date.today().strftime("%Y%m%d")
        resolved_ref = flow_universe_audit.get(
            "resolved_reference_business_day", integrated_universe_ref.strftime("%Y%m%d")
        )
        flow_batch_name = (
            f"DEV_UNIVERSE_FLOW_{target_count:03d}_REF{resolved_ref}_"
            f"{flow_batch_start:%Y%m%d}_{flow_batch_end:%Y%m%d}_"
            f"생성{created}{partial_suffix}.zip"
        )
        st.download_button(
            "Universe + 99종목 OHLCV·기관·외국인 수급 ZIP 다운로드",
            flow_payload,
            file_name=flow_batch_name,
            mime="application/zip",
            use_container_width=True,
        )
        st.caption(
            "ZIP에는 Universe snapshot/order/audit, 성공 종목별 결합 CSV, "
            "batch_manifest.csv, batch_audit.json, SHA256_MANIFEST.json이 들어 있습니다. "
            "실패·결측은 0으로 대체하지 않습니다."
        )

# ---------------------------------------------------------------------
# STOCK / INDEX — existing v0.9.9 data paths preserved
# ---------------------------------------------------------------------
else:
    if data_kind == "개별주식":
        q = st.text_input(
            "종목명 또는 6자리 종목코드",
            placeholder="예: SK하이닉스, 하이닉스, 펩트론 또는 000660",
        )
        include_investor_flow = st.checkbox(
            "기관·외국인 수급 함께 받기",
            value=True,
            help="같은 종목·기간의 OHLCV와 기관·외국인 수급을 한 번에 수집하고 날짜별로 결합합니다.",
        )
        index_sel = None
    else:
        index_sel = st.selectbox("시장지수", list(INDEX_PRESETS.keys()), index=0)
        q = ""
        include_investor_flow = False
        st.caption("KOSPI=1001 · KOSDAQ=2001. 시장지수는 FinanceDataReader 전용 경로를 사용합니다.")

    default_start = date(2021, 1, 4) if data_kind == "개별주식" else date(2000, 1, 1)
    a, b = st.columns(2)
    with a:
        s = st.date_input(
            "시작일", value=default_start,
            min_value=date(1900, 1, 1), max_value=date.today(),
        )
    with b:
        e = st.date_input(
            "종료일", value=date.today(),
            min_value=date(1900, 1, 1), max_value=date.today(),
        )

    if data_kind == "개별주식":
        source_mode = st.radio(
            "데이터 소스",
            [
                "KRX DIRECT RAW (권장)",
                "KRX pykrx RAW (구버전 진단)",
                "FDR 장기이력 후보 (대조 후 승인)",
                "NAVER FCHART (보조/대조용)",
            ],
            horizontal=True,
        )
        st.caption(
            "KRX DIRECT는 공식 MDCSTAT01701에 adjStkPrc=1(단순/비수정)로 요청합니다. "
            "빈 응답이면 선택적 KRX 로그인으로 다시 확인할 수 있습니다."
        )
        with st.expander(
            "KRX 로그인 (수급 함께 받기에는 필수)",
            expanded=include_investor_flow,
        ):
            st.caption(
                "2026년 KRX 정책상 투자자 수급 조회에는 로그인이 필요합니다. "
                "ID/비밀번호는 이번 실행 요청에만 사용하며 CSV/로그에 저장하지 않습니다. "
                "채팅에는 절대 보내지 마세요."
            )
            krx_id = st.text_input("KRX ID", value="", key="krx_id")
            krx_pw = st.text_input("KRX 비밀번호", value="", type="password", key="krx_pw")
    else:
        source_mode = "KRX INDEX"
        krx_id = ""
        krx_pw = ""
        st.info(
            "시장지수는 FinanceDataReader 지수 전용 경로를 사용합니다. "
            "개별주식 KRX DIRECT RAW와 완전히 분리하며 source=FDR_INDEX로 기록합니다."
        )

    run_label = "OHLCV + 기관·외국인 수급 CSV 만들기" if include_investor_flow else "OHLCV CSV 만들기"
    if st.button(run_label, type="primary", use_container_width=True):
        if s > e:
            st.error("날짜 범위를 확인하세요.")
            st.stop()

        if include_investor_flow and not (krx_id and krx_pw):
            st.error(
                "기관·외국인 수급에는 KRX ID와 비밀번호가 모두 필요합니다. "
                "위 로그인 칸에 입력한 뒤 다시 실행하세요."
            )
            st.stop()

        if include_investor_flow and not source_mode.startswith("KRX DIRECT"):
            st.error(
                "OHLCV+수급 결합 연구입력은 가격 원본을 통일하기 위해 "
                "데이터 소스를 'KRX DIRECT RAW (권장)'로 선택해야 합니다."
            )
            st.stop()

        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if include_investor_flow and e >= now_kst.date() and now_kst.hour < 18:
            st.error(
                f"오늘({now_kst.date()}) 수급은 아직 확정 전입니다. "
                f"종료일을 {(now_kst.date() - timedelta(days=1))} 이하로 바꾸거나 18:00 KST 이후 실행하세요."
            )
            st.stop()

        index_diag = None
        if data_kind == "개별주식":
            x = resolve(q)
            if not x:
                st.error("종목을 찾지 못했습니다. 종목명 또는 6자리 종목코드를 확인해 주세요.")
                st.stop()
        else:
            x = INDEX_PRESETS[index_sel].copy()

        try:
            diagnostics = None
            direct_diag = None
            fdr_diag = None
            if data_kind == "시장지수":
                df, index_diag = fetch_krx_index(x["code"], x["name"], s, e)
            elif source_mode.startswith("KRX DIRECT"):
                df, direct_diag = fetch_krx_direct_raw(x["code"], s, e, krx_id, krx_pw)
            elif source_mode.startswith("KRX pykrx"):
                df, diagnostics = fetch_krx_raw(x["code"], s, e)
            elif source_mode.startswith("FDR"):
                df, fdr_diag = fetch_fdr_candidate(x["code"], s, e)
            else:
                df = fetch_naver_fchart(x["code"], s, e)
        except Exception as ex:
            st.error(f"수집 실패: {type(ex).__name__}: {ex}")
            st.stop()

        if index_diag is not None:
            st.write(f"지수: **{x['name']} ({x['code']})**")
            st.write(f"FinanceDataReader 버전: **{index_diag.get('fdr_version', 'unknown')}**")
            idf = pd.DataFrame(index_diag.get("chunks", []))
            if not idf.empty:
                with st.expander("FDR INDEX 자동분할 수집 로그", expanded=True):
                    st.dataframe(idf, use_container_width=True, hide_index=True)
                    ok = int((idf["status"] == "INDEX_OK").sum())
                    empty = int((idf["status"] == "INDEX_EMPTY").sum())
                    err = int((idf["status"] == "INDEX_ERROR").sum())
                    st.write(f"성공 구간: **{ok}** / 빈 구간: **{empty}** / 오류 구간: **{err}**")
            if index_diag.get("status") != "INDEX_OK":
                st.warning("지수 수집 구간 중 실패가 있습니다. 완성 데이터로 사용하지 마세요.")

        if direct_diag is not None:
            st.write(f"종목 해석: **{x['name']} ({x['code']})**")
            st.write(
                f"KRX issue code: **{direct_diag.get('isin','(없음)')}** "
                f"({direct_diag.get('isin_status','')})"
            )
            if direct_diag.get("login_attempted"):
                if direct_diag.get("login_success"):
                    st.success("KRX 로그인 성공")
                else:
                    st.warning(
                        f"KRX 로그인 실패: {direct_diag.get('login_code','')} "
                        f"{direct_diag.get('login_message','')}"
                    )
            else:
                st.info("익명 KRX 세션으로 조회했습니다.")

            ddf = pd.DataFrame(direct_diag.get("chunks", []))
            if not ddf.empty:
                with st.expander("KRX DIRECT 분할수집 로그", expanded=True):
                    st.dataframe(ddf, use_container_width=True, hide_index=True)
                    ok = int((ddf["status"] == "RAW_OK").sum())
                    empty = int((ddf["status"] == "RAW_EMPTY").sum())
                    er = int((~ddf["status"].isin(["RAW_OK", "RAW_EMPTY"])).sum())
                    st.write(f"RAW 성공 구간: **{ok}** / 빈 구간: **{empty}** / 기타 오류: **{er}**")

            if direct_diag.get("isin_error") and direct_diag.get("isin_status") == "KNOWN_FALLBACK":
                st.caption(f"finder 응답이 없어 검증된 ISIN fallback 사용: {direct_diag.get('isin_error')}")

        if fdr_diag is not None:
            st.write(f"종목 해석: **{x['name']} ({x['code']})**")
            st.write(f"FinanceDataReader 버전: **{fdr_diag.get('fdr_version', 'unknown')}**")
            st.write(
                f"FDR 상태: **{fdr_diag.get('status','')}** / "
                f"최종 반환 행: **{fdr_diag.get('returned_rows', 0)}**"
            )
            if fdr_diag.get("actual_start"):
                st.write(
                    f"실제 범위: **{fdr_diag.get('actual_start')} ~ {fdr_diag.get('actual_end')}**"
                )
            cdf = pd.DataFrame(fdr_diag.get("chunks", []))
            if not cdf.empty:
                with st.expander("FDR 5년 분할수집 로그", expanded=True):
                    st.dataframe(cdf, use_container_width=True, hide_index=True)
            st.warning(
                "FDR 데이터는 현재 '장기이력 후보'입니다. "
                "기존 검증 원자료와 중첩구간 OHLCV를 대조하기 전에는 HRF OOS에 사용하지 않습니다."
            )

        if diagnostics is not None:
            st.write(f"종목 해석: **{x['name']} ({x['code']})**")
            st.write(f"pykrx 버전: **{diagnostics.get('pykrx_version', 'unknown')}**")
            ddf = pd.DataFrame(diagnostics.get("chunks", []))
            if not ddf.empty:
                with st.expander("KRX 수집 진단 로그", expanded=True):
                    st.dataframe(ddf, use_container_width=True, hide_index=True)

        if df.empty:
            st.error("선택한 소스에서 데이터가 없습니다. 위 진단 상태를 확인해 주세요.")
            st.stop()

        df = add_index_audit_columns(df) if data_kind == "시장지수" else add_audit_columns(df)
        actual_start = df.date.min().date()
        actual_end = df.date.max().date()
        warning_n = int((df.ohlc_warning != "").sum())
        invalid_n = int((~df.valid_session).sum())

        st.success(f'{x["name"]} ({x["code"]}) · {len(df):,}행 수집')
        st.write(f"실제 수집기간: {actual_start} ~ {actual_end}")
        st.write(f"소스: {df['source'].iloc[0]}")

        if actual_start > s:
            st.warning(
                f"요청 시작일은 {s}이지만 첫 수집일은 {actual_start}입니다. "
                "부족한 과거 구간을 자동 생성하지 않습니다."
            )
        if actual_end < e - timedelta(days=7):
            st.warning(f"요청 종료일 {e}보다 실제 마지막 수집일 {actual_end}이 이릅니다.")

        if warning_n:
            st.warning(f"원본 OHLC 관계 이상: {warning_n:,}행 — 값은 수정하지 않았습니다.")
            st.dataframe(
                df.loc[
                    df.ohlc_warning != "",
                    ["date", "open", "high", "low", "close", "volume", "ohlc_warning"],
                ],
                use_container_width=True,
                hide_index=True,
            )

        if invalid_n:
            st.info(f"HRF valid_session 제외 대상: {invalid_n:,}행 — 원본 행은 CSV에 보존됩니다.")

        z = df.copy()
        z["date"] = z.date.dt.strftime("%Y-%m-%d")
        payload = z.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

        partial = bool(index_diag and index_diag.get("status") != "INDEX_OK")
        filename = make_csv_filename(x["name"], df, partial=partial)

        st.download_button(
            "CSV 다운로드",
            payload,
            file_name=filename,
            mime="text/csv",
            use_container_width=True,
        )
        st.caption(
            f"파일명: {filename} · Safari 기본 다운로드 위치를 "
            "'나의 iPhone/GPT/주식시세추이'로 지정했다면 그 폴더에 저장됩니다."
        )

        if include_investor_flow:
            st.divider()
            st.subheader("기관·외국인 수급 및 OHLCV 결합")
            try:
                with st.spinner("동일 종목·기간의 KRX 투자자 수급을 로그인 세션으로 수집·검산하는 중입니다."):
                    flow, flow_diagnostics = fetch_investor_flow_by_date(
                        x["code"],
                        s,
                        e,
                        login_id=krx_id,
                        login_pw=krx_pw,
                    )
            except Exception as ex:
                st.error(f"수급 수집 실패: {type(ex).__name__}: {ex}")
                flow = pd.DataFrame()
                flow_diagnostics = {"status": "FLOW_EXCEPTION", "calls": []}

            st.write(
                f"수급 상태: **{flow_diagnostics.get('status', '')}** · "
                f"KRX 로그인: **{'성공' if flow_diagnostics.get('auth_success') else '실패'}** · "
                f"pykrx: **{flow_diagnostics.get('pykrx_version', 'unknown')}**"
            )
            flow_calls = pd.DataFrame(flow_diagnostics.get("calls", []))
            if not flow_calls.empty:
                with st.expander(
                    "KRX 수급 분할수집 로그",
                    expanded=flow_diagnostics.get("status") != "FLOW_OK",
                ):
                    st.dataframe(flow_calls, use_container_width=True, hide_index=True)

            if flow.empty:
                flow_status = flow_diagnostics.get("status", "")
                if flow_status == "FLOW_AUTH_REQUIRED":
                    st.error("KRX ID와 비밀번호가 없어 수급을 조회하지 못했습니다.")
                elif flow_status == "FLOW_AUTH_FAILED":
                    st.error("KRX 로그인이 거부됐습니다. ID·비밀번호 또는 비밀번호 변경 필요 여부를 확인하세요.")
                elif flow_status == "FLOW_EMPTY" and flow_diagnostics.get("auth_success"):
                    st.error(
                        "KRX 로그인은 성공했지만 수급 응답이 비었습니다. "
                        "빈 응답을 0으로 대체하거나 결합하지 않았습니다."
                    )
                else:
                    st.error(
                        f"수급을 만들지 못했습니다: {flow_status}. "
                        f"{flow_diagnostics.get('auth_error', '')}"
                    )
            else:
                flow_ok = flow_diagnostics.get("status") == "FLOW_OK"
                provisional_rows = int((~flow["flow_finalized"]).sum())
                if flow_ok:
                    st.success(f"수급 {len(flow):,}거래일 · 매수-매도=순매수 항등식 검산 통과")
                else:
                    st.error(
                        "일부 호출·스키마 또는 항등식 검산에 실패했습니다. "
                        "완성 연구 입력으로 사용하지 않습니다."
                    )
                if provisional_rows:
                    st.warning(
                        f"18:00 KST 전 당일 수급 {provisional_rows}행은 미확정입니다. "
                        "결합 연구파일 생성을 차단합니다."
                    )

                flow_summary = positive_net_cost_proxy_summary(flow)
                if not flow_summary.empty:
                    flow_summary_view = flow_summary.copy()
                    for column in (
                        "positive_net_addition_price_proxy",
                        "weighted_daily_price_dispersion",
                    ):
                        flow_summary_view[column] = flow_summary_view[column].round(2)
                    st.markdown("#### 선택 기간 양(+) 순매수 원가 후보")
                    st.dataframe(flow_summary_view, use_container_width=True, hide_index=True)

                preview_columns = [
                    "date",
                    "institution_net_qty",
                    "institution_net_value",
                    "institution_gross_buy_avg_price",
                    "foreign_net_qty",
                    "foreign_net_value",
                    "foreign_gross_buy_avg_price",
                    "flow_input_complete",
                    "flow_identity_ok",
                    "flow_finalized",
                ]
                st.markdown("#### 최근 수급")
                st.dataframe(flow[preview_columns].tail(30), use_container_width=True, hide_index=True)

                flow_export = flow.copy()
                flow_export["date"] = pd.to_datetime(flow_export["date"]).dt.strftime("%Y-%m-%d")
                flow_suffix = "" if flow_ok and provisional_rows == 0 else "_NOT_RESEARCH_READY"
                flow_payload = flow_export.to_csv(
                    index=False, encoding="utf-8-sig"
                ).encode("utf-8-sig")
                st.download_button(
                    "수급 원자료 CSV 다운로드",
                    flow_payload,
                    file_name=(
                        f"{filename_time_prefix()}_{x['code']}_{s:%Y%m%d}_{e:%Y%m%d}"
                        f"_investor_flow{flow_suffix}.csv"
                    ),
                    mime="text/csv",
                    use_container_width=True,
                )

                merged_flow, merge_diagnostics = merge_ohlcv_and_flow_exact(df, flow)
                if not flow_ok or provisional_rows:
                    st.error("수급 데이터가 완전 확정되지 않아 결합 연구파일 생성을 차단했습니다.")
                elif merge_diagnostics.get("status") != "MERGE_OK":
                    st.error(
                        f"결합 차단({merge_diagnostics.get('status')}): "
                        f"OHLCV {merge_diagnostics.get('ohlcv_rows', 0):,}행 · "
                        f"수급 {merge_diagnostics.get('flow_rows', 0):,}행 · "
                        f"공통 {merge_diagnostics.get('common_rows', 0):,}행. "
                        "누락값을 0으로 채우지 않았습니다."
                    )
                else:
                    merged_export = merged_flow.copy()
                    merged_export["date"] = pd.to_datetime(merged_export["date"]).dt.strftime("%Y-%m-%d")
                    merged_payload = merged_export.to_csv(
                        index=False, encoding="utf-8-sig"
                    ).encode("utf-8-sig")
                    combined_name = (
                        f"{filename_time_prefix()}_{safe_filename_piece(x['name'])}_"
                        f"{s:%Y%m%d}_{e:%Y%m%d}_OHLCV_INVESTOR_FLOW.csv"
                    )
                    st.success(
                        f"OHLCV와 수급 날짜 전부 일치: **{len(merged_flow):,}거래일** · 결합 완료"
                    )
                    st.download_button(
                        "OHLCV + 기관·외국인 수급 결합 CSV 다운로드",
                        merged_payload,
                        file_name=combined_name,
                        mime="text/csv",
                        use_container_width=True,
                    )

            st.caption(
                "원가 후보는 실제 보유잔고 원가가 아닙니다. HRF Living Map v1.0 CORE와 분리된 "
                "외부 진단 입력이며, OOS 검증 전에는 매매 신호로 사용하지 않습니다."
            )
