# v0.9.8 KRX DIRECT RAW + INDEX DIRECT CORE

## 변경점
- 기존 개별주식 KRX DIRECT RAW 기능 유지
- 시장지수(KOSPI 1001 / KOSDAQ 2001) 수집 기능 유지
- pykrx public index 함수에서 발생하는 `KeyError: '지수명'` 우회
- `pykrx.website.krx.market.core.개별지수시세`를 직접 호출
- 최대 700 calendar-day 단위 자동 분할 → 병합 → 중복 제거
- 각 분할 구간 method/status/error audit 표시
- 원자료 자동수정/보간 없음

## 먼저 테스트
1. 시장지수 → KOSPI
2. 2026-01-01 ~ 2026-08-27
3. 성공하면 2010-01-04 ~ 2026-08-27 전체 수집

## 파일
Streamlit 실행 파일은 반드시 `app.py` 입니다.
