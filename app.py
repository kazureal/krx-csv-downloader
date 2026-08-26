import re
import xml.etree.ElementTree as ET
from datetime import date
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.8", page_icon="📈")
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"}

@st.cache_data(ttl=3600, show_spinner=False)
def resolve_name(q):
    q = (q or "").strip()
    if re.fullmatch(r"\d{6}", q):
        return {"name": q, "code": q}
    try:
        r = requests.get(
            "https://ac.finance.naver.com/ac",
            params={"q": q, "q_enc": "UTF-8", "st": "111", "sug": "all", "frm": "stock"},
            headers=HEADERS, timeout=15
        )
        r.raise_for_status()
        d = r.json()
        groups = d.get("items") or []
        items = groups[0] if groups else []
        cand = []
        for x in items:
            if isinstance(x, list) and len(x) > 1:
                name, code = str(x[0]), str(x[1])
                if re.fullmatch(r"\d{6}", code):
                    cand.append({"name": name, "code": code})
        if cand:
            exact = [x for x in cand if x["name"].replace(" ","") == q.replace(" ","")]
            return (exact or cand)[0]
    except Exception:
        pass
    return None

def parse_xml(content):
    text = None
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        text = content.decode("utf-8", errors="replace")
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text, count=1, flags=re.I)
    return ET.fromstring(text)

def fetch(code, start, end):
    count = min(max(max((end-start).days, 1) * 2, 400), 20000)
    r = requests.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"},
        headers=HEADERS, timeout=30
    )
    r.raise_for_status()
    root = parse_xml(r.content)
    rows = []
    for it in root.iter("item"):
        raw = it.attrib.get("data", "")
        p = raw.split("|")
        if len(p) >= 6 and re.fullmatch(r"\d{8}", p[0]):
            rows.append(p[:6] + [raw])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date","open","high","low","close","volume","raw_data"])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date","open","high","low","close","volume"]).copy()
    df = df[(df.date.dt.date >= start) & (df.date.dt.date <= end)]
    df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype("int64")
    return df

def add_warnings(df):
    max_ocl = df[["open","close","low"]].max(axis=1)
    min_och = df[["open","close","high"]].min(axis=1)
    high_bad = df["high"] < max_ocl
    low_bad = df["low"] > min_och
    df = df.copy()
    df["ohlc_warning"] = ""
    df.loc[high_bad, "ohlc_warning"] = "HIGH_LT_MAX_OCL"
    df.loc[low_bad, "ohlc_warning"] = "LOW_GT_MIN_OCH"
    df.loc[high_bad & low_bad, "ohlc_warning"] = "HIGH_AND_LOW_RELATION_ERROR"
    df["source"] = "NAVER_FCHART"
    df["auto_corrected"] = False
    return df

st.title("Korea OHLCV CSV v0.8")
st.caption("단순 수집기 · 원본 보존 · 이상치 표시 · 자동보정 없음")
st.info("NAVER FCHART 차트 시세를 수집합니다. 과거 corporate action이 반영된 가격계열일 수 있으므로 당시 실제 비수정 체결가격과 동일하다고 가정하지 않습니다.")

q = st.text_input("종목명 또는 6자리 종목코드", placeholder="예: 펩트론 또는 087010")
a, b = st.columns(2)
with a:
    start = st.date_input("시작일", date(2000,1,1))
with b:
    end = st.date_input("종료일", date.today())

if st.button("OHLCV CSV 만들기", type="primary", use_container_width=True):
    if start > end:
        st.error("시작일이 종료일보다 늦습니다.")
        st.stop()

    x = resolve_name(q)
    if not x:
        st.error("종목명 변환에 실패했습니다. 6자리 종목코드를 입력해 주세요.")
        st.stop()

    try:
        df = fetch(x["code"], start, end)
    except Exception as e:
        st.error(f"수집 실패: {type(e).__name__}: {e}")
        st.stop()

    if df.empty:
        st.error("데이터가 없습니다.")
        st.stop()

    df = add_warnings(df)
    warning_count = int((df["ohlc_warning"] != "").sum())

    st.success(f'{x["name"]} ({x["code"]}) · {len(df):,}행 수집')
    st.write(f"기간: {df['date'].min().date()} ~ {df['date'].max().date()}")

    if warning_count:
        st.warning(f"원본 OHLC 관계 이상: {warning_count:,}행 — 값은 수정하지 않았습니다.")
        st.dataframe(
            df.loc[df["ohlc_warning"] != "", ["date","open","high","low","close","volume","ohlc_warning"]],
            use_container_width=True, hide_index=True
        )
    else:
        st.success("기본 OHLC 관계검사 이상 없음")

    export = df[["date","open","high","low","close","volume","ohlc_warning","source","auto_corrected"]].copy()
    export["date"] = export["date"].dt.strftime("%Y-%m-%d")
    payload = export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    fn = f'{x["code"]}_{df["date"].min():%Y%m%d}_{df["date"].max():%Y%m%d}_ohlcv.csv'

    st.download_button("CSV 다운로드", payload, fn, "text/csv", use_container_width=True)
