# Korea OHLCV CSV v1.0.10 — STOCK + INDEX + UNIVERSE

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


## v1.0.4 Universe hotfix
v1.0.3 실제 테스트에서도 current FDR `StockListing("KRX")`가 usable snapshot을 만들지 못해
known-broken pykrx current market-cap path로 fallback된 것이 확인되었습니다.

수정:
- current-date Universe는 `StockListing("KOSPI")`와 `StockListing("KOSDAQ")`를 각각 호출
- 각 listing의 실제 row 수와 column명을 `fdr_listing` audit에 기록
- current-date FDR listing 실패 시 pykrx current snapshot으로 조용히 fallback하지 않음
- 따라서 같은 pykrx schema error를 반복하지 않고 원인을 audit로 직접 노출
- historical date에서만 pykrx historical fallback 허용
- future outcome 봉인 유지

재테스트 시 실패하더라도 audit의 `fdr_listing` 항목만 보면 다음 원인을 정확히 확정할 수 있습니다.


## v1.0.5 deployment-cache fix
v1.0.4 재테스트 화면에 `snapshot_cap` 오류가 계속 표시되었습니다.
v1.0.4 current-date 코드라면 해당 stage가 나올 수 없으므로,
Streamlit이 이전 `universe_engine_v0_1.py`를 계속 사용 중인 것으로 판단했습니다.

이번 수정:
- 엔진 파일명을 `universe_engine_v0_1_5.py`로 변경
- `app.py` import도 새 파일명으로 변경
- 화면에 `BUILD: APP_v1.0.5 / UNIVERSE_ENGINE_v0.1.5` 표시
- audit JSON에 `engine_build=UNIVERSE_ENGINE_v0.1.5_20260827` 기록
- 실패 화면에도 실제 실행 엔진과 snapshot source를 먼저 표시

중요:
GitHub에서 기존 `universe_engine_v0_1.py`는 삭제하고,
새 `universe_engine_v0_1_5.py`를 올려야 합니다.


## v1.0.6 cast hotfix
실제 v1.0.5 실행에서:
`TypeError: cannot safely cast non-equivalent object to int64`
가 발생했습니다.

원인:
- market-cap / liquidity bucket을 만들 때 rank percentile * bucket_count가
  0.37, 1.82 같은 소수인데 pandas nullable `Int64`로 바로 cast함.
- pandas가 non-integral float -> Int64 safe cast를 거부.

수정:
- bucket index를 `np.floor()`로 명시적으로 내림한 뒤
- 0 ~ n-1 범위로 clip
- 그 다음 `Int64`로 변환

이 수정은 structural bucketing 코드만 변경하며
H15/MFE/MAE 등 future outcome은 계속 봉인됩니다.

배포 확인:
화면에
`BUILD: APP_v1.0.6 / UNIVERSE_ENGINE_v0.1.6`
가 보여야 합니다.


## v1.0.7 structural-universe fix
v1.0.6은 snapshot 자체는 성공했지만 audit에서 40개 오류가 확인되었습니다.
모두 pykrx 1.2.8의 전종목 일별 OHLCV 경로였고,
결과적으로 `median_trading_value_20d`가 전부 NaN이었습니다.
또한 가격 중심 FDR listing에는 Sector가 없어 모든 업종이 SECTOR_UNKNOWN이었습니다.

수정:
- `KOSPI-DESC` / `KOSDAQ-DESC`를 같은 날 descriptive metadata로 merge
- Sector / Industry / ListingDate 확보
- DESC membership을 current-date common-stock company cross-check로 사용
- 우선주/특수종목 이름패턴 필터도 유지
- broken pykrx all-market 20일 liquidity 호출 40개를 제거
- 20일 중위 거래대금은 선정된 각 종목의 검증 OHLCV에서 계산하도록 명시적으로 deferred
- current-day 거래대금으로 20일 median을 대체하지 않음
- selection order는 market + sector + market-cap bucket만 사용
- H15/MFE/MAE future outcome 봉인 유지


## v1.0.8 sector-definition fix
v1.0.7 audit 결과:
- KOSDAQ `Sector` = 우량기업부/중견기업부/벤처기업부 등 소속부 분류
- KOSPI `Sector` = 대부분 미제공
- `Industry`는 의약품 제조업, 반도체 제조업, 소프트웨어 개발 및 공급업 등 실제 경제 활동 분류

따라서:
- raw `Sector`는 `listing_board_sector`로 보존
- `Industry`는 원문 그대로 보존
- Development stratification에는 `selection_sector`를 새로 사용
- `selection_sector`는 Industry 텍스트를 outcome-blind deterministic keyword taxonomy로 broad mapping
- unmatched는 `OTHER_UNKNOWN`; 절대 삭제하지 않음
- selection_stratum = market | selection_sector | market-cap tercile
- liquidity는 계속 selected-stock OHLCV 단계에서 계산
- H15/MFE/MAE future outcome 봉인 유지


## v1.0.9 Development Batch OHLCV
새 모드: `Development Batch OHLCV`

입력:
- 승인된 Universe Audit Bundle ZIP
- 운영 배치 크기(기본 10, max 20)
- 배치 번호
- OHLCV 기간

원칙:
- `development_selection_order`를 그대로 사용
- 배치 크기는 서버 timeout 회피용 운영 단위이며 연구 표본수 기준이 아님
- 종목을 수동으로 골라 넣지 않음
- 기존 KRX DIRECT RAW endpoint / adjStkPrc=1 사용
- optional `ACC_TRDVAL`이 존재하면 trading_value 보존
- 각 종목 valid-session 기준 마지막 20개 trading_value의 median 계산
- trading_value가 충분하지 않으면 UNKNOWN_TRADING_VALUE_SUPPORT
- H15/MFE/MAE/tail outcome 계산 금지

출력 ZIP:
- `ohlcv/*.csv`
- `batch_manifest.csv`
- `batch_audit.json`
- `SHA256_MANIFEST.json`

권장 첫 실행:
- Universe ZIP: 승인된 v1.0.8 결과
- 배치 크기: 10
- 배치 번호: 1
- 기간: 2010-01-04 ~ 2026-08-27

첫 배치가 정상인지 확인한 뒤 다음 배치로 진행합니다.


## v1.0.10 Batch fetch-path reuse fix
Batch 1 live test에서 10/10 종목이 JSONDecodeError로 실패했습니다.
ISIN finder는 성공했으므로 Universe/selection 문제는 아니었습니다.

수정 원칙:
- 별도 `fetch_krx_direct_raw_batch()` 구현을 사용하지 않음
- Batch가 기존 개별주식에서 실제 성공한 `fetch_krx_direct_raw()`를 그대로 호출
- 종목 간 1초 operational pacing 추가
- `valid_session`은 수집 후 structural audit column으로 계산
- verified single-stock path가 OHLCV만 반환하므로 trading value는 추정하지 않음
- `median_trading_value_20d`는 `PENDING_VERIFIED_TRADING_VALUE_SOURCE`
- close*volume 같은 대체값 사용 금지
- H15/MFE/MAE 등 future outcome은 계속 봉인

첫 재시험:
- 배치 크기 10
- 배치 번호 1
- 2010-01-04 ~ 2026-08-27
- KRX 로그인 비움
