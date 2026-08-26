# Korea OHLCV CSV v0.7

NAVER FCHART 원본을 보존하면서 금융위원회_주식시세정보(data.go.kr)를 별도 열로 교차검증하는 버전.

Streamlit Secrets:
DATA_GO_KR_SERVICE_KEY = "공공데이터포털에서 발급받은 서비스키"

원칙:
- NAVER 값 자동 수정 금지
- 공공데이터 값도 별도 열로 보존
- exact OHLC / OHLCV match를 구분
- 단순 불일치는 SOURCE_CONFLICT_OR_BASIS_DIFFERENT로 표시
- 내부 OHLC 정상 + 공공데이터 OHLCV 완전일치만 research_ready=True
- 공공데이터 키가 없거나 조회 실패하면 research_ready=False

공식 API:
금융위원회_주식시세정보
GetStockSecuritiesInfoService/getStockPriceInfo
