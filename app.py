import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.9 RAW", page_icon="📈")

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


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_krx_raw(code, start, end):
    """KRX 비수정(adjusted=False) OHLCV. 긴 기간은 연도 단위로 분할 요청한다."""
    try:
        from pykrx import stock
    except Exception as ex:
        raise RuntimeError("pykrx를 불러오지 못했습니다. requirements.txt 설치 상태를 확인하세요.") from ex

    pieces = []
    cur = start
    while cur <= end:
        chunk_end = min(date(cur.year, 12, 31), end)
        df = stock.get_market_ohlcv_by_date(
            cur.strftime("%Y%m%d"),
            chunk_end.strftime("%Y%m%d"),
            code,
            adjusted=False,
        )
        if df is not None and not df.empty:
            x = df.reset_index()
            # pykrx 한글 컬럼명 -> 표준명
            x = x.rename(columns={
                x.columns[0]: "date",
                "시가": "open",
                "고가": "high",
                "저가": "low",
                "종가": "close",
                "거래량": "volume",
            })
            pieces.append(x[["date", "open", "high", "low", "close", "volume"]])
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.08)

    if not pieces:
        return _standardize(pd.DataFrame(), "KRX_RAW")
    return _standardize(pd.concat(pieces, ignore_index=True), "KRX_RAW")


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


st.title("Korea OHLCV CSV v0.9 RAW")
st.caption("종목명/6자리 코드 · KRX 비수정 OHLCV 우선 · 원본 보존 · 이상치 표시 · 자동보정 없음")

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
    ["KRX RAW (연구용 권장)", "NAVER FCHART (보조/대조용)"],
    horizontal=True,
)

if st.button("OHLCV CSV 만들기", type="primary", use_container_width=True):
    if s > e:
        st.error("날짜 범위를 확인하세요.")
        st.stop()

    x = resolve(q)
    if not x:
        st.error("종목을 찾지 못했습니다. 종목명 또는 6자리 코드를 확인해 주세요.")
        st.stop()

    try:
        if source_mode.startswith("KRX"):
            df = fetch_krx_raw(x["code"], s, e)
        else:
            df = fetch_naver_fchart(x["code"], s, e)
    except Exception as ex:
        st.error(f"수집 실패: {type(ex).__name__}: {ex}")
        st.stop()

    if df.empty:
        st.error("데이터가 없습니다.")
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
