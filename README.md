# v0.9.9 KRX DIRECT RAW + FDR INDEX

## 변경점
- 기존 개별주식 `KRX DIRECT RAW` 기능 유지
- 시장지수는 KRX/pykrx 직접 호출 대신 FinanceDataReader 사용
  - KOSPI: `KS11`
  - KOSDAQ: `KQ11`
- 긴 기간은 3년 이하 조각으로 자동 분할 → 병합 → 날짜 중복 제거
- 시장지수 CSV에는 `source=FDR_INDEX`를 명시
- 지수의 `trading_value`, `market_cap`은 소스가 제공하지 않으면 비워 둠
- 원자료 자동수정/보간 없음
- 분할 구간별 method/status/error 진단 유지

## 테스트 순서
1. 시장지수 → KOSPI
2. 2026-01-01 ~ 2026-08-27
3. 성공 확인
4. 2010-01-04 ~ 2026-08-27 전체 수집

## Streamlit
실행 파일은 `app.py` 입니다.
