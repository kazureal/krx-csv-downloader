import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.9.5 KRX DIRECT RAW", page_icon="📈")

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


st.title("Korea OHLCV CSV v0.9.5 KRX DIRECT RAW")
st.caption("KRX 직접 비수정 OHLCV · 선택적 KRX 로그인 · FDR/NAVER 대조 · 원본 보존 · 자동보정 없음")

q = st.text_input(
    "종목명 또는 6자리 종목코드",
    placeholder="예: SK하이닉스, 하이닉스, 펩트론 또는 000660",
)

a, b = st.columns(2)
with a:
    s = st.date_input("시작일", date(2000, 1, 1))
with b:
    e = st.date_input("종료일", date.today())

source_mode = st.radio(
    "데이터 소스",
    ["KRX DIRECT RAW (권장)", "KRX pykrx RAW (구버전 진단)", "FDR 장기이력 후보 (대조 후 승인)", "NAVER FCHART (보조/대조용)"],
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

if st.button("OHLCV CSV 만들기", type="primary", use_container_width=True):
    if s > e:
        st.error("날짜 범위를 확인하세요.")
        st.stop()

    x = resolve(q)
    if not x:
        st.error("종목을 찾지 못했습니다. 종목명 또는 6자리 코드를 확인해 주세요.")
        st.stop()

    try:
        diagnostics = None
        direct_diag = None
        fdr_diag = None
        if source_mode.startswith("KRX DIRECT"):
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
                ok = int((cdf["status"] == "FDR_OK").sum())
                empty = int((cdf["status"] == "FDR_EMPTY").sum())
                err = int((cdf["status"] == "FDR_ERROR").sum())
                st.write(f"성공 구간: **{ok}** / 빈 구간: **{empty}** / 오류 구간: **{err}**")

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
                raw_ok = int((ddf["status"] == "RAW_OK").sum())
                raw_empty_adj = int((ddf["status"] == "RAW_EMPTY_ADJ_EXISTS").sum())
                raw_err = int((ddf["status"] == "RAW_ERROR").sum())
                st.write(
                    f"RAW 성공 구간: **{raw_ok}** / "
                    f"RAW 비었지만 adjusted probe 존재: **{raw_empty_adj}** / "
                    f"RAW 오류 구간: **{raw_err}**"
                )

    if df.empty:
        st.error("선택한 소스에서 데이터가 없습니다. 위 진단 상태를 확인해 주세요.")
        if source_mode.startswith("KRX DIRECT"):
            if not krx_id:
                st.info(
                    "익명 KRX 조회가 빈 응답이면 위 'KRX 로그인'을 열어 "
                    "본인의 KRX 계정으로 다시 시도하세요. ID/비밀번호는 채팅에 보내지 마세요."
                )
            else:
                st.info(
                    "로그인까지 성공했는데도 비면 KRX의 과거 데이터 제공 범위/권한 문제로 판단합니다."
                )
        elif source_mode.startswith("KRX pykrx"):
            st.info(
                "중요: adjusted=True probe는 '데이터 존재 여부' 진단용일 뿐이며, "
                "CSV에는 사용하지 않습니다."
            )
        st.stop()

    df = add_audit_columns(df)
    actual_start = df.date.min().date()
    actual_end = df.date.max().date()
    warning_n = int((df.ohlc_warning != "").sum())
    invalid_n = int((~df.valid_session).sum())

    st.success(f'{x["name"]} ({x["code"]}) · {len(df):,}행 수집')
    st.write(f"실제 수집기간: {actual_start} ~ {actual_end}")
    st.write(f"소스: {df['source'].iloc[0]}")

    # 요청 범위를 서버가 다 주지 못했는지 명확하게 경고한다.
    if actual_start > s:
        st.warning(
            f"요청 시작일은 {s}이지만 첫 수집일은 {actual_start}입니다. "
            "데이터 공급원/상장·기업행위 이력/조회 제한을 확인하세요. 부족한 과거 구간을 자동 생성하지 않습니다."
        )
    if actual_end < e - timedelta(days=7):
        st.warning(f"요청 종료일 {e}보다 실제 마지막 수집일 {actual_end}이 이릅니다.")

    if warning_n:
        st.warning(f"원본 OHLC 관계 이상: {warning_n:,}행 — 값은 수정하지 않았습니다.")
        st.dataframe(
            df.loc[df.ohlc_warning != "", ["date", "open", "high", "low", "close", "volume", "ohlc_warning"]],
            use_container_width=True,
            hide_index=True,
        )

    if invalid_n:
        st.info(f"HRF valid_session 제외 대상: {invalid_n:,}행 — 원본 행은 CSV에 보존됩니다.")

    z = df.copy()
    z["date"] = z.date.dt.strftime("%Y-%m-%d")
    payload = z.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "CSV 다운로드",
        payload,
        f'{x["code"]}_{df.date.min():%Y%m%d}_{df.date.max():%Y%m%d}_ohlcv.csv',
        "text/csv",
        use_container_width=True,
    )
