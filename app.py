import streamlit as st
import pandas as pd
from pykrx import stock
from datetime import date, timedelta
import hashlib, io, time

st.set_page_config(page_title="KRX Raw CSV", page_icon="📈", layout="centered")
st.title("KRX Raw OHLCV CSV")
st.caption("데이터 취득 전용 v0.1 · 연구 규칙/분석 기능 없음")

@st.cache_data(ttl=86400)
def ticker_table():
    rows=[]
    for market in ["KOSPI","KOSDAQ","KONEX"]:
        try:
            for t in stock.get_market_ticker_list(market=market):
                rows.append((t, stock.get_market_ticker_name(t), market))
        except Exception:
            pass
    return pd.DataFrame(rows, columns=["Ticker","Name","Market"]).drop_duplicates()

q=st.text_input("종목명 또는 6자리 종목코드", placeholder="예: SK하이닉스 또는 000660")
c1,c2=st.columns(2)
start=c1.date_input("시작일", date(2000,1,1))
end=c2.date_input("종료일", date.today())

def resolve(q):
    q=q.strip()
    if len(q)==6 and q.isdigit():
        return q, stock.get_market_ticker_name(q) or q
    tbl=ticker_table()
    exact=tbl[tbl.Name.str.lower()==q.lower()]
    if len(exact)==1: return exact.iloc[0].Ticker, exact.iloc[0].Name
    partial=tbl[tbl.Name.str.contains(q, case=False, regex=False)]
    if len(partial)==1: return partial.iloc[0].Ticker, partial.iloc[0].Name
    return None, partial

def fetch_chunks(ticker, start, end):
    # KRX 화면의 장기 조회 제한과 서버 부담을 피하기 위해 2년 미만으로 자동 분할.
    cur=start
    frames=[]
    while cur<=end:
        stop=min(end, cur + timedelta(days=700))
        df=stock.get_market_ohlcv(cur.strftime("%Y%m%d"), stop.strftime("%Y%m%d"),
                                  ticker, adjusted=False)
        if df is not None and not df.empty:
            frames.append(df)
        cur=stop+timedelta(days=1)
        time.sleep(0.25)
    if not frames: return pd.DataFrame()
    out=pd.concat(frames)
    out=out[~out.index.duplicated(keep="first")].sort_index()
    return out

if st.button("KRX Raw CSV 만들기", type="primary", use_container_width=True):
    if not q.strip():
        st.error("종목명을 입력해 주세요.")
    elif start>end:
        st.error("시작일이 종료일보다 늦습니다.")
    else:
        resolved=resolve(q)
        if resolved[0] is None:
            p=resolved[1]
            if len(p):
                st.warning("종목이 여러 개 검색됩니다. 6자리 종목코드로 입력해 주세요.")
                st.dataframe(p.head(20), hide_index=True)
            else:
                st.error("종목을 찾지 못했습니다.")
        else:
            ticker,name=resolved
            with st.spinner(f"{name} ({ticker}) KRX Raw OHLCV 수집 중..."):
                try:
                    df=fetch_chunks(ticker,start,end)
                    if df.empty:
                        st.error("KRX에서 데이터가 반환되지 않았습니다.")
                    else:
                        df=df.reset_index()
                        rename={"날짜":"Date","시가":"Open","고가":"High","저가":"Low",
                                "종가":"Close","거래량":"Volume","거래대금":"Value","등락률":"Change"}
                        df=df.rename(columns=rename)
                        # VALID_SESSION_RULE은 적용하지 않는다. 앱은 원자료 취득만 담당.
                        qc={
                            "rows":len(df),
                            "duplicate_dates":int(df["Date"].duplicated().sum()),
                            "volume_zero":int((df["Volume"]==0).sum()) if "Volume" in df else None,
                        }
                        csv=df.to_csv(index=False).encode("utf-8-sig")
                        digest=hashlib.sha256(csv).hexdigest()
                        st.success(f"{name} ({ticker}) · {len(df):,}행")
                        st.write("QC:", qc)
                        st.code("SHA-256: "+digest)
                        st.dataframe(df.tail(10), hide_index=True)
                        fn=f"{ticker}_{name}_KRX_RAW_{start:%Y%m%d}_{end:%Y%m%d}.csv"
                        st.download_button("CSV 다운로드", csv, fn, "text/csv",
                                           use_container_width=True)
                        st.caption("Source: KRX via pykrx adjusted=False. 수정주가/네이버 데이터와 혼합하지 않음.")
                except Exception as e:
                    st.error("수집 실패: "+str(e))
                    st.info("KRX/pykrx 연결 상태 문제일 수 있습니다. 데이터 소스를 자동으로 다른 곳으로 바꾸지 않습니다.")
