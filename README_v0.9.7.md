# v0.9.7 STOCK + INDEX

기존 v0.9.6 개별주식 수집 기능은 유지하고 시장지수 수집 모드를 추가했습니다.

## 추가 기능
- `수집 대상`: `개별주식` / `시장지수`
- 시장지수 preset
  - KOSPI = `1001`
  - KOSDAQ = `2001`
- 장기 지수 조회를 앱 내부에서 700일 이하 구간으로 자동 분할
- 분할 결과 자동 병합 / 날짜 정렬 / 중복일 제거
- 각 chunk별 상태, 실제 시작/종료일, 사용 method, 오류를 audit log로 표시
- KRX 지수 OHLCV 외에 가능한 경우 `trading_value`, `market_cap`도 보존
- 지수 포인트 OHLC는 소수점(float) 그대로 보존
- 자동 보정/보간/backfill 없음
- 일부 chunk 실패 시 전체 상태를 `INDEX_PARTIAL`로 표시하고 다운로드 파일명에 `_PARTIAL` 추가

## 기존 기능 보존
- KRX DIRECT RAW 개별주식 경로 (`MDCSTAT01701`, `adjStkPrc=1`)
- 선택적 KRX 로그인
- pykrx RAW 진단 경로
- FDR 장기이력 후보
- NAVER FCHART 보조/대조
- OHLC warning / valid_session audit

## 권장 M3 수집 예시
- 수집 대상: `시장지수`
- 지수: `KOSPI`
- 시작일: `2010-01-04`
- 종료일: 원하는 최신일
- 생성 파일 예: `KOSPI_20100104_20260827_index.csv`

## QA 권장
1. 짧은 구간(예: 2026-01-01~2026-08-27) KOSPI 다운로드
2. 긴 구간(2010-01-04~최신일) 다운로드
3. 분할 로그가 모두 `INDEX_OK`인지 확인
4. 날짜 중복이 없는지 확인
5. `INDEX_PARTIAL` 또는 `_PARTIAL.csv`이면 연구 완성 데이터로 사용하지 않음

## 배포
GitHub 저장소의 기존 `app.py`를 `app_v0.9.7.py` 내용으로 교체하고 기존 `requirements.txt`는 그대로 사용하면 됩니다.
