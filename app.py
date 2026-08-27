import re
import time
import io
import json
import zipfile
import hashlib
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

from universe_engine_v0_1 import UniverseConfig, build_universe

st.set_page_config(page_title="Korea OHLCV CSV v1.0.3 STOCK + INDEX + UNIVERSE", page_icon="📈")

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
    return f"{safe_filename_piece(name)}_{start}_{end}_생성{created}{suffix}.csv"


st.title("Korea OHLCV CSV v1.0.3 STOCK + INDEX + UNIVERSE")
st.caption(
    "개별주식 KRX DIRECT RAW + KOSPI/KOSDAQ 지수(FDR) + "
    "Track 02 Development Universe · 원본 보존 · outcome-blind"
)

data_kind = st.radio(
    "수집 대상",
    ["개별주식", "시장지수", "Development Universe"],
    horizontal=True,
)

# ---------------------------------------------------------------------
# DEVELOPMENT UNIVERSE
# ---------------------------------------------------------------------
if data_kind == "Development Universe":
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
        "고정 설계: KOSPI+KOSDAQ · KRX 업종 · 시장별 시총 tercile · "
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
            st.error("KRX/pykrx에서 universe를 가져오지 못했습니다.")
            st.json(diag)
            st.stop()

        st.success(
            f"기준 영업일 {diag.get('resolved_reference_business_day')} · "
            f"snapshot {len(full):,}종목 · common-stock candidate "
            f"{int(full['eligible_common_candidate'].sum()):,}종목"
        )

        if diag.get("errors"):
            st.warning(
                f"수집 오류 {len(diag['errors'])}건. audit JSON을 확인하고 "
                "불완전 데이터로 final selection을 진행하지 마세요."
            )
            with st.expander("Universe 오류 로그"):
                st.dataframe(pd.DataFrame(diag["errors"]), use_container_width=True, hide_index=True)

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
            "development_selection_order", "ticker", "name", "market", "sector",
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

        manifest = {
            "universe_snapshot.csv": hashlib.sha256(full_bytes).hexdigest(),
            "development_selection_order.csv": hashlib.sha256(order_bytes).hexdigest(),
            "universe_audit.json": hashlib.sha256(audit_bytes).hexdigest(),
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("universe_snapshot.csv", full_bytes)
            zf.writestr("development_selection_order.csv", order_bytes)
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
# STOCK / INDEX — existing v0.9.9 data paths preserved
# ---------------------------------------------------------------------
else:
    if data_kind == "개별주식":
        q = st.text_input(
            "종목명 또는 6자리 종목코드",
            placeholder="예: SK하이닉스, 하이닉스, 펩트론 또는 000660",
        )
        index_sel = None
    else:
        index_sel = st.selectbox("시장지수", list(INDEX_PRESETS.keys()), index=0)
        q = ""
        st.caption("KOSPI=1001 · KOSDAQ=2001. 시장지수는 FinanceDataReader 전용 경로를 사용합니다.")

    a, b = st.columns(2)
    with a:
        s = st.date_input(
            "시작일", value=date(2000, 1, 1),
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
        with st.expander("KRX 로그인 (선택 사항 — 빈 응답일 때만 사용)", expanded=False):
            st.caption(
                "ID/비밀번호는 이 앱 실행 중 KRX 로그인 요청에만 사용하며 CSV/로그에 저장하지 않습니다. "
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

    if st.button("OHLCV CSV 만들기", type="primary", use_container_width=True):
        if s > e:
            st.error("날짜 범위를 확인하세요.")
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
