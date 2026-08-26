import re, xml.etree.ElementTree as ET
from datetime import date
import pandas as pd, requests, streamlit as st

st.set_page_config(page_title="Korea OHLCV CSV v0.7", page_icon="📈")
HEADERS={"User-Agent":"Mozilla/5.0","Accept-Language":"ko-KR,ko;q=0.9"}
DATA_URL="https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"

@st.cache_data(ttl=3600, show_spinner=False)
def resolve_name(q):
    q=(q or "").strip()
    if re.fullmatch(r"\d{6}",q): return {"name":q,"code":q}
    try:
        r=requests.get("https://ac.finance.naver.com/ac",
          params={"q":q,"q_enc":"UTF-8","st":"111","sug":"all","frm":"stock"},headers=HEADERS,timeout=15)
        r.raise_for_status(); d=r.json()
        groups=d.get("items") or []; items=groups[0] if groups else []; cand=[]
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

def fetch_naver(code,start,end):
    count=min(max(max((end-start).days,1)*2,400),20000)
    r=requests.get("https://fchart.stock.naver.com/sise.nhn",
      params={"symbol":code,"timeframe":"day","count":count,"requestType":"0"},headers=HEADERS,timeout=30)
    r.raise_for_status(); root=parse_xml(r.content); rows=[]
    for it in root.iter("item"):
        raw=it.attrib.get("data",""); p=raw.split("|")
        if len(p)>=6 and re.fullmatch(r"\d{8}",p[0]): rows.append(p[:6]+[raw])
    df=pd.DataFrame(rows,columns=["date","open","high","low","close","volume","raw_data"])
    if df.empty:return df
    df["date"]=pd.to_datetime(df["date"],format="%Y%m%d",errors="coerce")
    for c in ["open","high","low","close","volume"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    df=df.dropna().copy()
    df=df[(df.date.dt.date>=start)&(df.date.dt.date<=end)].drop_duplicates("date").sort_values("date")
    for c in ["open","high","low","close","volume"]:df[c]=df[c].astype("int64")
    return df.reset_index(drop=True)

def secret_key():
    try:return st.secrets.get("DATA_GO_KR_SERVICE_KEY","")
    except Exception:return ""

def fetch_datagokr(code,start,end,key):
    # Official API supports basDt begin/end and likeSrtnCd. Keep source basis separate from NAVER.
    params={
      "serviceKey":key,"resultType":"json","numOfRows":"10000","pageNo":"1",
      "beginBasDt":start.strftime("%Y%m%d"),"endBasDt":end.strftime("%Y%m%d"),
      "likeSrtnCd":code
    }
    r=requests.get(DATA_URL,params=params,timeout=60); r.raise_for_status()
    try:d=r.json()
    except Exception: raise RuntimeError("공공데이터 응답이 JSON이 아닙니다: "+r.text[:200])
    hdr=d.get("response",{}).get("header",{})
    if str(hdr.get("resultCode","00")) not in ("00","0"):
        raise RuntimeError(f'{hdr.get("resultCode")}: {hdr.get("resultMsg")}')
    body=d.get("response",{}).get("body",{})
    items=body.get("items",{}).get("item",[])
    if isinstance(items,dict):items=[items]
    rows=[]
    for x in items:
        srtn=str(x.get("srtnCd",""))
        # API may return A087010 while user code is 087010.
        if code not in srtn: continue
        rows.append({
          "date":x.get("basDt"),"public_open":x.get("mkp"),"public_high":x.get("hipr"),
          "public_low":x.get("lopr"),"public_close":x.get("clpr"),
          "public_volume":x.get("trqu"),"public_srtnCd":srtn
        })
    df=pd.DataFrame(rows)
    if df.empty:return df
    df["date"]=pd.to_datetime(df["date"],format="%Y%m%d",errors="coerce")
    for c in ["public_open","public_high","public_low","public_close","public_volume"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    return df.dropna(subset=["date"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)

def internal_bad(df):
    if df.empty:return pd.Series(dtype=bool)
    return (df.high<df[["open","close","low"]].max(axis=1))|(df.low>df[["open","close","high"]].min(axis=1))

st.title("Korea OHLCV CSV v0.7")
st.caption("NAVER 원본 보존 + 금융위원회/공공데이터 KRX계열 교차검증")
st.warning("서로 가격기준이 다를 수 있으므로 값이 다르다고 자동 보정하지 않습니다.")

q=st.text_input("종목명 또는 6자리 코드",placeholder="예: 펩트론 또는 087010")
a,b=st.columns(2)
with a:start=st.date_input("시작일",date(2000,1,1))
with b:end=st.date_input("종료일",date.today())

if st.button("수집·교차검증",type="primary",use_container_width=True):
    if start>end:st.error("날짜 범위를 확인하세요.");st.stop()
    x=resolve_name(q)
    if not x:st.error("종목명 변환 실패. 6자리 코드를 입력하세요.");st.stop()
    try:nv=fetch_naver(x["code"],start,end)
    except Exception as e:st.error(f"NAVER 수집 실패: {type(e).__name__}: {e}");st.stop()
    if nv.empty:st.error("NAVER 데이터가 없습니다.");st.stop()

    nv["naver_internal_ohlc_ok"]=~internal_bad(nv)
    nv["naver_price_basis"]="ADJUSTED_OR_CHART_BASIS"
    nv["auto_corrected"]=False

    key=secret_key()
    pub=pd.DataFrame()
    public_error=""
    if key:
        try:pub=fetch_datagokr(x["code"],start,end,key)
        except Exception as e:public_error=f"{type(e).__name__}: {e}"

    z=nv.copy()
    if not pub.empty:
        z=z.merge(pub,on="date",how="left")
        public_cols=["public_open","public_high","public_low","public_close","public_volume"]
        z["public_data_present"]=z["public_close"].notna()
        price_equal=(z["open"]==z["public_open"])&(z["high"]==z["public_high"])&(z["low"]==z["public_low"])&(z["close"]==z["public_close"])
        volume_equal=z["volume"]==z["public_volume"]
        z["exact_ohlc_match"]=z["public_data_present"]&price_equal
        z["exact_ohlcv_match"]=z["exact_ohlc_match"]&volume_equal
        z["crosscheck_status"]="NO_PUBLIC_DATA"
        z.loc[z.public_data_present,"crosscheck_status"]="SOURCE_CONFLICT_OR_BASIS_DIFFERENT"
        z.loc[z.exact_ohlc_match,"crosscheck_status"]="CROSSCHECK_OHLC_MATCH"
        z.loc[z.exact_ohlcv_match,"crosscheck_status"]="CROSSCHECK_OHLCV_MATCH"
        # Strict rule: only internally valid NAVER rows with exact public OHLCV match are research-ready.
        z["research_ready"]=z["naver_internal_ohlc_ok"]&z["exact_ohlcv_match"]
    else:
        z["public_data_present"]=False
        z["exact_ohlc_match"]=False
        z["exact_ohlcv_match"]=False
        z["crosscheck_status"]="NOT_CHECKED"
        z["research_ready"]=False

    st.write(f'{x["name"]} ({x["code"]}) · NAVER {len(nv):,}행')
    bad=int((~z.naver_internal_ohlc_ok).sum())
    st.write(f"NAVER 내부 OHLC 이상: {bad:,}행")

    if not key:
        st.info("공공데이터 서비스키가 없어 NAVER 원본만 저장합니다. Streamlit Secrets에 DATA_GO_KR_SERVICE_KEY를 넣으면 자동 교차검증됩니다.")
    elif public_error:
        st.error("공공데이터 교차검증 실패: "+public_error)
    else:
        st.write(f"공공데이터 매칭 가능 행: {int(z.public_data_present.sum()):,}")
        st.write(f"OHLC 완전일치: {int(z.exact_ohlc_match.sum()):,}")
        st.write(f"OHLCV 완전일치: {int(z.exact_ohlcv_match.sum()):,}")
        st.write(f"research_ready=True: {int(z.research_ready.sum()):,}")

    badview=z[~z.naver_internal_ohlc_ok].copy()
    if not badview.empty:
        st.subheader(f"NAVER 내부 이상행 ({len(badview)}행)")
        st.dataframe(badview,use_container_width=True,hide_index=True)

    z["date"]=z["date"].dt.strftime("%Y-%m-%d")
    payload=z.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("검증 CSV 다운로드",payload,f'{x["code"]}_{start:%Y%m%d}_{end:%Y%m%d}_v07.csv',"text/csv",use_container_width=True)
