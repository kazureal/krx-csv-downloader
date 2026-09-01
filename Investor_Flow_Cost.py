import io
from datetime import date

import pandas as pd
import streamlit as st

from investor_flow import (
    FLOW_ENGINE_VERSION,
    fetch_investor_flow_by_date,
    normalize_ticker_input,
    positive_net_cost_proxy_summary,
)


st.set_page_config(page_title="기관·외국인 수급 원가 연구", page_icon="🧭", layout="wide")


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_flow_cached(ticker, start, end):
    return fetch_investor_flow_by_date(ticker, start, end)


def read_ohlcv_upload(uploaded_file):
    raw = uploaded_file.getvalue()
    parsed = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            parsed = pd.read_csv(io.BytesIO(raw), encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if parsed is None:
        raise ValueError("OHLCV CSV 인코딩을 읽지 못했습니다.")

    parsed.columns = [str(column).strip().lower() for column in parsed.columns]
    parsed = parsed.rename(
        columns={"날짜": "date", "시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"}
    )
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in parsed.columns]
    if missing:
        raise ValueError(f"OHLCV 필수 열 누락: {missing}")
    parsed = parsed[required].copy()
    parsed["date"] = pd.to_datetime(parsed["date"], errors="coerce")
    for column in required[1:]:
        parsed[column] = pd.to_numeric(parsed[column], errors="coerce")
    return parsed.dropna(subset=required).sort_values("date").drop_duplicates("date", keep="last")


st.title("기관·외국인 수급 원가 연구 입력")
st.caption(f"{FLOW_ENGINE_VERSION} · HRF Living Map v1.0 CORE와 분리된 외부 진단 데이터")

st.info(
    "KRX 투자자별 거래실적을 이용해 기관합계·외국인합계의 일별 매수/매도/순매수 "
    "수량과 금액을 수집합니다. 이 페이지는 HRF S1/NEXT를 변경하지 않습니다."
)

query = st.text_input("종목명 또는 6자리 종목코드", value="삼성전자")
left, right = st.columns(2)
with left:
    start = st.date_input("수급 시작일", value=date(2021, 1, 1), min_value=date(1990, 1, 1), max_value=date.today())
with right:
    end = st.date_input("수급 종료일", value=date.today(), min_value=date(1990, 1, 1), max_value=date.today())

ohlcv_upload = st.file_uploader(
    "기존 앱에서 받은 OHLCV CSV 연결 (선택)",
    type=["csv"],
    help="업로드하면 날짜 기준으로 수급과 병합한 연구용 CSV도 함께 만듭니다.",
)

st.warning(
    "표시되는 원가 값은 실제 보유잔고 원가가 아닙니다. 선택 기간의 양(+) 순매수일에 대해 "
    "일별 매수금액÷매수수량을 양(+) 순매수수량으로 가중한 연구용 후보치입니다."
)

if st.button("기관·외국인 수급 새로 받기", type="primary", use_container_width=True):
    if start > end:
        st.error("시작일과 종료일을 확인하세요.")
        st.stop()

    try:
        ticker, name = normalize_ticker_input(query)
    except Exception as ex:
        st.error(str(ex))
        st.stop()

    try:
        with st.spinner("KRX 투자자별 수량·금액을 구간별로 수집하고 검산하는 중입니다."):
            flow, diagnostics = fetch_flow_cached(ticker, start, end)
    except Exception as ex:
        st.error(f"수급 수집 실패: {type(ex).__name__}: {ex}")
        st.stop()

    st.write(f"종목: **{name} ({ticker})**")
    st.write(
        f"상태: **{diagnostics.get('status', '')}** · "
        f"pykrx: **{diagnostics.get('pykrx_version', 'unknown')}**"
    )

    calls = pd.DataFrame(diagnostics.get("calls", []))
    if not calls.empty:
        with st.expander("KRX 수급 분할수집 로그", expanded=diagnostics.get("status") != "FLOW_OK"):
            st.dataframe(calls, use_container_width=True, hide_index=True)

    if flow.empty:
        st.error("수급 응답이 비었습니다. 빈 응답을 0으로 대체하지 않았습니다.")
        st.stop()

    if diagnostics.get("status") != "FLOW_OK":
        st.error(
            "일부 수급 호출·스키마·매수-매도=순매수 검산에 실패했습니다. "
            "이 파일은 완성 연구 입력으로 사용하지 마세요."
        )
    else:
        st.success(f"{len(flow):,}개 거래일 수급 수집 및 항등식 검산 통과")

    provisional = int((~flow["flow_finalized"]).sum())
    if provisional:
        st.warning(
            f"오후 6시 이전 당일 수급 {provisional}행은 provisional입니다. "
            "종가 연구에는 18:00 KST 이후 다시 받으세요."
        )

    summary = positive_net_cost_proxy_summary(flow)
    if not summary.empty:
        summary_view = summary.copy()
        for column in ("positive_net_addition_price_proxy", "weighted_daily_price_dispersion"):
            summary_view[column] = summary_view[column].round(2)
        st.markdown("#### 선택 기간 양(+) 순매수 원가 후보")
        st.dataframe(summary_view, use_container_width=True, hide_index=True)

    preview_columns = [
        "date",
        "institution_net_qty",
        "institution_net_value",
        "institution_gross_buy_avg_price",
        "foreign_net_qty",
        "foreign_net_value",
        "foreign_gross_buy_avg_price",
        "flow_input_complete",
        "flow_identity_ok",
        "flow_finalized",
    ]
    st.markdown("#### 최근 수급")
    st.dataframe(flow[preview_columns].tail(30), use_container_width=True, hide_index=True)

    export = flow.copy()
    export["date"] = pd.to_datetime(export["date"]).dt.strftime("%Y-%m-%d")
    flow_payload = export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "수급 원자료 CSV 다운로드",
        flow_payload,
        f"{ticker}_{start:%Y%m%d}_{end:%Y%m%d}_investor_flow.csv",
        "text/csv",
        use_container_width=True,
    )

    if ohlcv_upload is not None:
        try:
            ohlcv = read_ohlcv_upload(ohlcv_upload)
            merged = ohlcv.merge(flow, on="date", how="inner", validate="one_to_one")
            if merged.empty:
                st.error("OHLCV와 수급의 공통 날짜가 없습니다.")
            else:
                merged_export = merged.copy()
                merged_export["date"] = merged_export["date"].dt.strftime("%Y-%m-%d")
                merged_payload = merged_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                st.write(
                    f"OHLCV {len(ohlcv):,}행과 수급 {len(flow):,}행 중 "
                    f"공통 거래일 **{len(merged):,}행**을 결합했습니다."
                )
                st.download_button(
                    "OHLCV + 수급 연구입력 CSV 다운로드",
                    merged_payload,
                    f"{ticker}_{start:%Y%m%d}_{end:%Y%m%d}_ohlcv_investor_flow.csv",
                    "text/csv",
                    use_container_width=True,
                )
        except Exception as ex:
            st.error(f"OHLCV 병합 실패: {type(ex).__name__}: {ex}")

st.divider()
st.caption(
    "연구 경계: 단순 가격 평균과 순매수금액÷순매수수량은 실제 원가로 사용하지 않습니다. "
    "수급 Episode 시작·종료 규칙과 HRF 구조 반응 검증을 통과하기 전에는 매매 신호로 승격하지 않습니다."
)

