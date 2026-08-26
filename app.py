import re, xml.etree.ElementTree as ET
from datetime import date
import pandas as pd, requests, streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.8 FINAL",page_icon="📈")
H={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9"}
ALIASES={"펩트론":"087010","삼성전자":"005930","디앤디파마텍":"347850","코오롱티슈진":"950160"}
REV={v:k for k,v in ALIASES.items()}

@st.cache_data(ttl=3600,show_spinner=False)
def resolve(q):
    q=(q or "").strip()
    if re.fullmatch(r"\d{6}",q): return {"name":REV.get(q,q),"code":q}
    key=q.replace(" ","")
    for n,c in ALIASES.items():
        if n.replace(" ","")==key:return {"name":n,"code":c}
    try:
        r=requests.get("https://ac.finance.naver.com/ac",
          params={"q":q,"q_enc":"UTF-8","st":"111","sug":"all","frm":"stock"},headers=H,timeout=15)
        r.raise_for_status(); text=r.text
        for pat in (re.escape(q)+r'.{0,250}?(?:A)?(\d{6})',r'(?:A)?(\d{6}).{0,250}?'+re.escape(q)):
            m=re.search(pat,text,re.S)
            if m:return {"name":q,"code":m.group(1)}
    except Exception:pass
    return None

def xmlroot(b):
    t=None
    for e in ("euc-kr","cp949","utf-8"):
        try:t=b.decode(e);break
        except UnicodeDecodeError:pass
    t=t if t is not None else b.decode("utf-8",errors="replace")
    t=re.sub(r'^\s*<\?xml[^>]*\?>','',t,count=1,flags=re.I)
    return ET.fromstring(t)

def fetch(code,start,end):
    count=min(max(max((end-start).days,1)*2,400),20000)
    r=requests.get("https://fchart.stock.naver.com/sise.nhn",
      params={"symbol":code,"timeframe":"day","count":count,"requestType":"0"},headers=H,timeout=30)
    r.raise_for_status(); rows=[]
    for it in xmlroot(r.content).iter("item"):
        p=it.attrib.get("data","").split("|")
        if len(p)>=6 and re.fullmatch(r"\d{8}",p[0]):rows.append(p[:6])
    df=pd.DataFrame(rows,columns=["date","open","high","low","close","volume"])
    if df.empty:return df
    df["date"]=pd.to_datetime(df.date,format="%Y%m%d",errors="coerce")
    for c in ["open","high","low","close","volume"]:df[c]=pd.to_numeric(df[c],errors="coerce")
    df=df.dropna().copy()
    df=df[(df.date.dt.date>=start)&(df.date.dt.date<=end)].drop_duplicates("date").sort_values("date")
    for c in ["open","high","low","close","volume"]:df[c]=df[c].astype("int64")
    return df.reset_index(drop=True)

st.title("Korea OHLCV CSV v0.8 FINAL")
st.caption("종목명/6자리 코드 · 원본 보존 · 이상치 표시 · 자동보정 없음")
q=st.text_input("종목명 또는 6자리 종목코드",placeholder="예: 펩트론 또는 087010")
a,b=st.columns(2)
with a:s=st.date_input("시작일",date(2000,1,1))
with b:e=st.date_input("종료일",date.today())

if st.button("OHLCV CSV 만들기",type="primary",use_container_width=True):
    if s>e:st.error("날짜 범위를 확인하세요.");st.stop()
    x=resolve(q)
    if not x:st.error("종목을 찾지 못했습니다. 6자리 코드를 입력해 주세요.");st.stop()
    try:df=fetch(x["code"],s,e)
    except Exception as ex:st.error(f"수집 실패: {type(ex).__name__}: {ex}");st.stop()
    if df.empty:st.error("데이터가 없습니다.");st.stop()
    hi=df.high<df[["open","close","low"]].max(axis=1)
    lo=df.low>df[["open","close","high"]].min(axis=1)
    df["ohlc_warning"]=""
    df.loc[hi,"ohlc_warning"]="HIGH_LT_MAX_OCL"
    df.loc[lo,"ohlc_warning"]="LOW_GT_MIN_OCH"
    df.loc[hi&lo,"ohlc_warning"]="HIGH_AND_LOW_RELATION_ERROR"
    df["source"]="NAVER_FCHART";df["auto_corrected"]=False
    n=int((df.ohlc_warning!="").sum())
    st.success(f'{x["name"]} ({x["code"]}) · {len(df):,}행 수집')
    st.write(f"기간: {df.date.min().date()} ~ {df.date.max().date()}")
    if n:
        st.warning(f"원본 OHLC 관계 이상: {n:,}행 — 값은 수정하지 않았습니다.")
        st.dataframe(df.loc[df.ohlc_warning!="",["date","open","high","low","close","volume","ohlc_warning"]],use_container_width=True,hide_index=True)
    z=df.copy();z["date"]=z.date.dt.strftime("%Y-%m-%d")
    payload=z.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("CSV 다운로드",payload,f'{x["code"]}_{df.date.min():%Y%m%d}_{df.date.max():%Y%m%d}_ohlcv.csv',"text/csv",use_container_width=True)
