import re
import xml.etree.ElementTree as ET
from datetime import date
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.2", page_icon="📈")
HEADERS={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9"}

@st.cache_data(ttl=3600, show_spinner=False)
def resolve_name(q):
    q=(q or "").strip()
    if re.fullmatch(r"\d{6}",q): return {"name":q,"code":q}
    for url,params in [
      ("https://ac.finance.naver.com/ac",{"q":q,"q_enc":"UTF-8","st":"111","sug":"all","frm":"stock"}),
      ("https://m.stock.naver.com/front-api/search/autoComplete",{"query":q,"target":"stock,index,marketindicator,coin,ipo"})]:
        try:
            r=requests.get(url,params=params,headers=HEADERS,timeout=15); r.raise_for_status(); d=r.json()
            if "ac.finance" in url:
                items=(d.get("items") or [[]])[0]
                c=[{"name":str(x[0]),"code":str(x[1])} for x in items if isinstance(x,list) and len(x)>1 and re.fullmatch(r"\d{6}",str(x[1]))]
            else:
                root=d.get("result",d); items=root.get("items",[]) if isinstance(root,dict) else []
                c=[]
                for x in items:
                    if isinstance(x,dict):
                        code=str(x.get("code") or x.get("itemCode") or x.get("symbolCode") or "")
                        name=str(x.get("name") or x.get("stockName") or q)
                        if re.fullmatch(r"\d{6}",code): c.append({"name":name,"code":code})
            if c:
                exact=[x for x in c if x["name"].replace(" ","")==q.replace(" ","")]
                return (exact or c)[0]
        except Exception: pass
    return None

def fetch(code,start,end):
    days=max((end-start).days,1); count=min(max(days*2,400),20000)
    r=requests.get("https://fchart.stock.naver.com/sise.nhn",
        params={"symbol":code,"timeframe":"day","count":count,"requestType":"0"},
        headers=HEADERS,timeout=30); r.raise_for_status()
    root=ET.fromstring(r.content); rows=[]
    for it in root.iter("item"):
        p=it.attrib.get("data","").split("|")
        if len(p)>=6 and re.fullmatch(r"\d{8}",p[0]): rows.append(p[:6])
    if not rows:return pd.DataFrame()
    df=pd.DataFrame(rows,columns=["date","open","high","low","close","volume"])
    df["date"]=pd.to_datetime(df["date"],format="%Y%m%d",errors="coerce")
    for c in ["open","high","low","close","volume"]:
        df[c]=pd.to_numeric(df[c].astype(str).str.replace(",","",regex=False),errors="coerce")
    df=df.dropna().query("@start <= date.dt.date <= @end").drop_duplicates("date",keep="last").sort_values("date").reset_index(drop=True)
    for c in ["open","high","low","close","volume"]:df[c]=df[c].astype("int64")
    return df

def validate(df):
    e=[]; w=[]
    if df.empty:return ["데이터가 0행입니다."],[]
    if df["date"].duplicated().any():e.append("날짜 중복")
    bad=(df["high"]<df[["open","close","low"]].max(axis=1))|(df["low"]>df[["open","close","high"]].min(axis=1))
    if bad.any():e.append(f"OHLC 관계 오류 {int(bad.sum())}행")
    if (df[["open","high","low","close"]]<=0).any().any():e.append("0 이하 가격")
    if (df["volume"]<0).any():e.append("음수 거래량")
    if len(df)<240:w.append(f"240거래일 미만: {len(df)}행")
    return e,w

st.title("Korea Raw OHLCV CSV")
st.caption("데이터 취득 전용 v0.2 · 연구 규칙/분석 기능 없음")
st.info("v0.2는 국내 네이버증권 공개 시세를 1차 수집원으로 사용합니다. KRX 직접 원자료라고 표시하지 않습니다.")
q=st.text_input("종목명 또는 6자리 종목코드",placeholder="예: 펩트론 또는 087010")
a,b=st.columns(2)
with a:start=st.date_input("시작일",date(2000,1,1))
with b:end=st.date_input("종료일",date.today())

if st.button("OHLCV CSV 만들기",type="primary",use_container_width=True):
    if start>end:st.error("시작일이 종료일보다 늦습니다.");st.stop()
    with st.spinner("수집 중..."):
        x=resolve_name(q)
        if not x:st.error("종목을 찾지 못했습니다. 6자리 코드로 다시 시도해 주세요.");st.stop()
        try:df=fetch(x["code"],start,end)
        except Exception as ex:st.error(f"시세 수집 실패: {type(ex).__name__}");st.stop()
    e,w=validate(df)
    if e:
        st.error("검증 실패 — CSV 생성을 중단했습니다: "+" / ".join(e));st.stop()
    st.success(f'{x["name"]} ({x["code"]}) · {len(df):,}행')
    for z in w:st.warning(z)
    st.dataframe(df.tail(10),use_container_width=True,hide_index=True)
    z=df.copy();z["date"]=z["date"].dt.strftime("%Y-%m-%d");z["primary_source"]="NAVER_FCHART";z["crosscheck_source"]="";z["crosscheck_ok"]=""
    payload=z.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
    fn=f'{x["code"]}_{df["date"].min():%Y%m%d}_{df["date"].max():%Y%m%d}_raw.csv'
    st.download_button("CSV 다운로드",payload,fn,"text/csv",use_container_width=True)
    st.caption("crosscheck 필드는 의도적으로 비워 둡니다. 연구 투입 전 별도 교차검증이 필요합니다.")
