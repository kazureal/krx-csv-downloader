# Korea OHLCV CSV v1.0.3 — STOCK + INDEX + UNIVERSE

## 이번 업데이트
기존 v0.9.9의 동작을 유지하면서 아래를 추가했습니다.

### 1) Development Universe 모드
- KOSPI + KOSDAQ point-in-time snapshot
- KRX 업종
- 기준일 시가총액
- 직전 20거래일 중위 거래대금
- 시장/업종/시총 tercile 기반 deterministic selection order
- Track 02 outcome-blind Development universe expansion용

### 2) CSV 파일명 변경
주식/지수 모두:
`이름_실제시작일_실제종료일_생성일.csv`

예:
- `SK하이닉스_20030414_20260827_생성20260827.csv`
- `KOSPI_20100104_20260827_생성20260827.csv`

지수 일부 수집 실패 시:
`KOSPI_20100104_20260827_생성20260827_PARTIAL.csv`

### 3) Universe ZIP 파일명
`UNIVERSE_기준YYYYMMDD_생성YYYYMMDD.zip`

### 4) iPhone 저장 위치
웹앱은 iPhone 파일시스템 경로를 직접 지정할 수 없습니다.
Safari 기본 다운로드 위치를
`나의 iPhone/GPT/주식시세추이`
로 지정하면 다운로드 버튼의 CSV/ZIP이 그 폴더로 저장됩니다.

## 기존 기능 보존
- 개별주식 KRX DIRECT RAW (권장)
- 선택적 KRX 로그인
- pykrx RAW 진단
- FDR 장기이력 후보
- NAVER FCHART 보조
- KOSPI/KOSDAQ FDR_INDEX
- OHLC warning / valid_session
- 원본 자동보정 없음

## Streamlit
GitHub의 기존 `app.py`를 이 패키지의 `app.py`로 교체하고,
`universe_engine_v0_1.py`와 `requirements.txt`도 같은 저장소 루트에 두세요.

실행:
`streamlit run app.py`

## 중요
- Universe 모드는 H15/MFE/MAE 등 미래 outcome을 읽지 않습니다.
- 우선주 판별은 현재 보수적 이름패턴 1차 필터이며, final universe freeze 전 KRX security-class cross-check가 필요합니다.


## v1.0.1 Universe hotfix
2026-08-27 실제 iPhone/Streamlit 테스트에서 Development Universe 생성 시
`IndexError: index -1 is out of bounds for axis 0 with size 0`가 발생했습니다.

원인 후보를 `pykrx.get_nearest_business_day_in_a_week()`의 빈 응답 처리로 좁혔고,
다음처럼 수정했습니다.

- 기준 영업일: 기존 앱에서 실제 검증된 FDR KOSPI(`KS11`) 거래일 calendar 사용
- 직전 20거래일: FDR KOSPI 실제 거래일 사용
- FDR calendar 실패 시에만 pykrx KOSPI 시총 데이터를 날짜별 backward probe
- 임의 평일 추정은 하지 않음
- Universe snapshot/sector/market-cap/trading-value 본체는 기존 pykrx 구조 유지

이 수정은 future H15/MFE/MAE outcome을 사용하지 않습니다.


## v1.0.2 Universe hotfix
실제 Streamlit 테스트에서 기준 영업일/20거래일 계산은 정상화되었지만,
pykrx 1.2.8의 `get_market_sector_classifications()` 내부에서
`KeyError: '종가'`가 KOSPI/KOSDAQ 모두 발생했습니다.

수정:
- Universe 본체는 `get_market_cap_by_ticker()`로 먼저 구성
- 업종 API 실패가 전체 Universe 생성을 중단시키지 않게 변경
- 같은 날(current reference date)인 경우에만 FDR KRX listing으로 종목명/업종 fallback
- historical reference date에서는 current-sector fallback을 쓰지 않고 `SECTOR_UNKNOWN`
- 따라서 과거 기준일에 현재 업종을 섞는 look-ahead를 금지
- H15/MFE/MAE 등 미래 outcome은 계속 사용하지 않음


## v1.0.3 Universe hotfix
실제 v1.0.2 테스트에서 pykrx 1.2.8의 `get_market_cap_by_ticker()` 반환 schema가
예상한 `종가/시가총액/거래량/거래대금` 열과 달라 Universe snapshot이 비었습니다.

수정:
- 기준일이 오늘이면 FinanceDataReader `StockListing("KRX")`를 current-date universe snapshot의 1차 소스로 사용
- Code/Name/Market/Sector/Close/Volume/Amount/Marcap/Stocks 계열을 schema-flexible하게 인식
- 과거 기준일에는 current listing 사용 금지
- pykrx market-cap fallback도 Korean/English schema 모두 허용
- 20거래일 거래대금 parser도 Korean/English trading-value column을 동적으로 인식
- future response outcome은 계속 봉인

이 버전은 현재 날짜 기준 Development Universe 생성 성공 여부를 다시 확인하기 위한 hotfix입니다.
