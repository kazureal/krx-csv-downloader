import re
import xml.etree.ElementTree as ET
from datetime import date
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.6", page_icon="📈")
HEADERS={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9"}

@st.cache_data(ttl=3600, show_spinner=False)
def resolve_name(q):
    q=(q or "").strip()
    if re.fullmatch(r"\d{6}",q): return {"name":q,"code":q}
    try:
        r=requests.get("https://ac.finance.naver.com/ac",
            params={"q":q,"q_enc":"UTF-8","st":"111","sug":"all","frm":"stock"},
            headers=HEADERS,timeout=15); r.raise_for_status()
        d=r.json(); groups=d.get("items") or []; items=groups[0] if groups else []
        cand=[]
        for x in items:
            if isinstance(x,list) and len(x)>1:
                name,code=str(x[0]),str(x[1])
                if re.fullmatch(r"\d{6}",code): cand.append({"name":name,"code":code})
        if cand:
            exact=[x for x in cand if x["name"].replace(" ","")==q.replace(" ","")]
            return (exact or cand)[0]
    except Exception: pass
    return None

def parse_xml(content):
    text=None
    for enc in ("euc-kr","cp949","utf-8"):
        try: text=content.decode(enc); break
        except UnicodeDecodeError: pass
    if text is None: text=content.decode("utf-8",errors="replace")
    text=re.sub(r'^\s*<\?xml[^>]*\?>','',text,count=1,flags=re.I)
    return ET.fromstring(text)

def fetch_naver_adjusted(code,start,end):
    days=max((end-start).days,1); count=min(max(days*2,400),20000)
    r=requests.get("https://fchart.stock.naver.com/sise.nhn",
        params={"symbol":code,"timeframe":"day","count":count,"requestType":"0"},
        headers=HEADERS,timeout=30); r.raise_for_status()
    root=parse_xml(r.content); rows=[]
    for it in root.iter("item"):
        raw=it.attrib.get("data",""); p=raw.split("|")
        if len(p)>=6 and re.fullmatch(r"\d{8}",p[0]): rows.append(p[:6]+[raw])
    if not rows: return pd.DataFrame()
    df=pd.DataFrame(rows,columns=["date","open","high","low","close","volume","raw_data"])
    df["date"]=pd.to_datetime(df["date"],format="%Y%m%d",errors="coerce")
    for c in ["open","high","low","close","volume"]:
        df[c]=pd.to_numeric(df[c].astype(str).str.replace(",","",regex=False),errors="coerce")
    df=df.dropna(subset=["date","open","high","low","close","volume"])
    df=df[(df.date.dt.date>=start)&(df.date.dt.date<=end)].copy()
    df=df.drop_duplicates("date",keep="last").sort_values("date").reset_index(drop=True)
    for c in ["open","high","low","close","volume"]: df[c]=df[c].astype("int64")
    df["source"]="NAVER_FCHART"
    df["price_basis"]="ADJUSTED_OR_CHART_BASIS"
    return df

def validate(df):
    errors=[]
    if df.empty: return ["데이터가 0행입니다."],pd.DataFrame()
    bad=(df["high"]<df[["open","close","low"]].max(axis=1)) | (df["low"]>df[["open","close","high"]].min(axis=1))
    diag=df.loc[bad].copy()
    if bad.any(): errors.append(f"OHLC 관계 오류 {int(bad.sum())}행")
    if df["date"].duplicated().any(): errors.append("날짜 중복")
    if not df["date"].is_monotonic_increasing: errors.append("날짜 정렬 오류")
    if (df[["open","high","low","close"]]<=0).any().any(): errors.append("0 이하 가격")
    if (df["volume"]<0).any(): errors.append("음수 거래량")
    return errors,diag

st.title("Korea OHLCV CSV v0.6")
st.caption("원자료 보존형 · 수정/비수정 가격을 섞지 않음")
st.info("NAVER FCHART는 차트/수정주가 계열로 취급합니다. OHLC 오류를 +1원 등으로 자동보정하지 않습니다.")

q=st.text_input("종목명 또는 6자리 종목코드",placeholder="예: 펩트론 또는 087010")
a,b=st.columns(2)
with a: start=st.date_input("시작일",date(2000,1,1))
with b: end=st.date_input("종료일",date.today())

if st.button("수집·검증",type="primary",use_container_width=True):
    if start>end: st.error("시작일이 종료일보다 늦습니다."); st.stop()
    x=resolve_name(q)
    if not x: st.error("종목을 찾지 못했습니다. 6자리 코드로 다시 시도해 주세요."); st.stop()
    try: df=fetch_naver_adjusted(x["code"],start,end)
    except Exception as ex: st.error(f"수집 실패: {type(ex).__name__}: {ex}"); st.stop()

    errors,diag=validate(df)
    st.write(f'종목: {x["name"]} ({x["code"]})')
    st.write(f"수집 행수: {len(df):,}")
    if not df.empty: st.write(f"기간: {df.date.min().date()} ~ {df.date.max().date()}")

    # provenance fields: never silently overwrite source values
    z=df.copy()
    z["source_open"]=z["open"]; z["source_high"]=z["high"]; z["source_low"]=z["low"]; z["source_close"]=z["close"]; z["source_volume"]=z["volume"]
    z["verified_open"]=pd.NA; z["verified_high"]=pd.NA; z["verified_low"]=pd.NA; z["verified_close"]=pd.NA; z["verified_volume"]=pd.NA
    z["crosscheck_source"]=""; z["crosscheck_status"]="NOT_CHECKED"
    z["auto_corrected"]=False
    z["research_ready"]=False

    if errors:
        st.error("검증 실패: "+" / ".join(errors))
        if not diag.empty:
            st.subheader(f"OHLC 오류 상세 ({len(diag)}행)")
            show=diag[["date","open","high","low","close","volume","raw_data"]].copy()
            show["date"]=show["date"].dt.strftime("%Y-%m-%d")
            st.dataframe(show,use_container_width=True,hide_index=True)
        st.warning("원자료는 보존합니다. 독립 출처 검증 전 research_ready=False입니다.")
    else:
        st.success("내부 OHLC 검증 통과. 단, 독립 출처 교차검증 전에는 research_ready=False입니다.")

    z["date"]=z["date"].dt.strftime("%Y-%m-%d")
    payload=z.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("원자료+검증상태 CSV 다운로드",payload,
        f'{x["code"]}_{start:%Y%m%d}_{end:%Y%m%d}_v06.csv',"text/csv",use_container_width=True)
