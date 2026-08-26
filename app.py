import re
import xml.etree.ElementTree as ET
from datetime import date
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.5", page_icon="📈")
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "ko-KR,ko;q=0.9"
}

@st.cache_data(ttl=3600, show_spinner=False)
def resolve_name(q):
    q = (q or "").strip()
    if re.fullmatch(r"\d{6}", q):
        return {"name": q, "code": q}

    try:
        r = requests.get(
            "https://ac.finance.naver.com/ac",
            params={"q": q, "q_enc": "UTF-8", "st": "111", "sug": "all", "frm": "stock"},
            headers=HEADERS,
            timeout=15,
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
            exact = [x for x in cand if x["name"].replace(" ", "") == q.replace(" ", "")]
            return (exact or cand)[0]
    except Exception:
        pass

    try:
        r = requests.get(
            "https://m.stock.naver.com/front-api/search/autoComplete",
            params={"query": q, "target": "stock,index,marketindicator,coin,ipo"},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        root = d.get("result", d)
        items = root.get("items", []) if isinstance(root, dict) else []
        cand = []
        for x in items:
            if isinstance(x, dict):
                code = str(x.get("code") or x.get("itemCode") or x.get("symbolCode") or "")
                name = str(x.get("name") or x.get("stockName") or q)
                if re.fullmatch(r"\d{6}", code):
                    cand.append({"name": name, "code": code})
        if cand:
            exact = [x for x in cand if x["name"].replace(" ", "") == q.replace(" ", "")]
            return (exact or cand)[0]
    except Exception:
        pass

    return None

def parse_naver_xml(content: bytes):
    text = None
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("utf-8", errors="replace")
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text, count=1, flags=re.I)
    return ET.fromstring(text)

def fetch(code, start, end):
    days = max((end - start).days, 1)
    count = min(max(days * 2, 400), 20000)

    r = requests.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()

    root = parse_naver_xml(r.content)

    rows = []
    for it in root.iter("item"):
        raw = it.attrib.get("data", "")
        p = raw.split("|")
        if len(p) >= 6 and re.fullmatch(r"\d{8}", p[0]):
            rows.append({
                "raw_data": raw,
                "date": p[0],
                "open": p[1],
                "high": p[2],
                "low": p[3],
                "close": p[4],
                "volume": p[5],
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(
            df[c].astype(str).str.replace(",", "", regex=False),
            errors="coerce"
        )

    df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"])

    mask = (df["date"].dt.date >= start) & (df["date"].dt.date <= end)
    df = df.loc[mask].copy()

    df = (
        df.drop_duplicates("date", keep="last")
          .sort_values("date")
          .reset_index(drop=True)
    )

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype("int64")

    return df

def diagnose_ohlc(df):
    if df.empty:
        return pd.DataFrame()

    max_ocl = df[["open", "close", "low"]].max(axis=1)
    min_och = df[["open", "close", "high"]].min(axis=1)

    high_bad = df["high"] < max_ocl
    low_bad = df["low"] > min_och
    bad = high_bad | low_bad

    diag = df.loc[bad, ["date", "open", "high", "low", "close", "volume", "raw_data"]].copy()
    diag["high_lt_required_max"] = high_bad.loc[bad].values
    diag["required_high_min"] = max_ocl.loc[bad].values
    diag["low_gt_required_min"] = low_bad.loc[bad].values
    diag["required_low_max"] = min_och.loc[bad].values

    def reason(row):
        reasons = []
        if row["high_lt_required_max"]:
            reasons.append("HIGH < max(O,C,L)")
        if row["low_gt_required_min"]:
            reasons.append("LOW > min(O,C,H)")
        return " + ".join(reasons)

    diag["failure_reason"] = diag.apply(reason, axis=1)
    return diag

def validate(df):
    errors, warnings = [], []
    if df.empty:
        return ["데이터가 0행입니다."], []

    if df["date"].duplicated().any():
        errors.append("날짜 중복")

    if not df["date"].is_monotonic_increasing:
        errors.append("날짜 정렬 오류")

    diag = diagnose_ohlc(df)
    if not diag.empty:
        errors.append(f"OHLC 관계 오류 {len(diag)}행")

    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        errors.append("0 이하 가격")

    if (df["volume"] < 0).any():
        errors.append("음수 거래량")

    if len(df) < 240:
        warnings.append(f"240거래일 미만: {len(df)}행")

    return errors, warnings

st.title("Korea Raw OHLCV CSV")
st.caption("데이터 취득 전용 v0.5 진단판 · 연구 규칙/분석 기능 없음")
st.info("네이버증권 공개 시세를 1차 수집원으로 사용합니다. KRX 직접 원자료라고 표시하지 않습니다.")

q = st.text_input("종목명 또는 6자리 종목코드", placeholder="예: 펩트론 또는 087010")
a, b = st.columns(2)
with a:
    start = st.date_input("시작일", date(2000, 1, 1))
with b:
    end = st.date_input("종료일", date.today())

if st.button("OHLCV 진단 실행", type="primary", use_container_width=True):
    if start > end:
        st.error("시작일이 종료일보다 늦습니다.")
        st.stop()

    with st.spinner("수집 및 진단 중..."):
        x = resolve_name(q)
        if not x:
            st.error("종목을 찾지 못했습니다. 6자리 코드로 다시 시도해 주세요.")
            st.stop()

        try:
            df = fetch(x["code"], start, end)
        except Exception as ex:
            st.error(f"시세 수집 실패: {type(ex).__name__}: {ex}")
            st.stop()

    st.write(f"종목: {x['name']} ({x['code']})")
    st.write(f"수집 행수: {len(df):,}")

    if not df.empty:
        st.write(f"수집 기간: {df['date'].min().date()} ~ {df['date'].max().date()}")

    errors, warnings = validate(df)
    diag = diagnose_ohlc(df)

    if errors:
        st.error("검증 실패: " + " / ".join(errors))

        if not diag.empty:
            st.subheader(f"OHLC 오류 상세 ({len(diag)}행)")
            st.caption("아래 행은 삭제·보정하지 않은 진단용 표시입니다.")
            show = diag.copy()
            show["date"] = show["date"].dt.strftime("%Y-%m-%d")
            st.dataframe(
                show[
                    [
                        "date", "open", "high", "low", "close", "volume",
                        "failure_reason", "required_high_min", "required_low_max",
                        "raw_data"
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

            diag_csv = show.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            st.download_button(
                "진단 CSV 다운로드",
                diag_csv,
                file_name=f'{x["code"]}_OHLC_DIAGNOSTIC.csv',
                mime="text/csv",
                use_container_width=True,
            )

        st.warning("진단판에서는 검증 실패 데이터의 일반 CSV 다운로드를 허용하지 않습니다.")
        st.stop()

    for w in warnings:
        st.warning(w)

    st.success("기본 OHLC 검증 통과")
    st.dataframe(df.tail(10).drop(columns=["raw_data"]), use_container_width=True, hide_index=True)

    z = df.drop(columns=["raw_data"]).copy()
    z["date"] = z["date"].dt.strftime("%Y-%m-%d")
    z["primary_source"] = "NAVER_FCHART"
    z["crosscheck_source"] = ""
    z["crosscheck_ok"] = ""

    payload = z.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    fn = f'{x["code"]}_{df["date"].min():%Y%m%d}_{df["date"].max():%Y%m%d}_raw.csv'

    st.download_button(
        "CSV 다운로드",
        payload,
        fn,
        "text/csv",
        use_container_width=True
    )
