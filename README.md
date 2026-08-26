# Korea Raw OHLCV CSV v0.3

데이터 취득 전용 앱. 연구/백테스트 로직과 분리.

## v0.3 fix
v0.2에서 발생한 pandas ValueError를 수정:
- `DataFrame.query()` 안에서 `date.dt.date` 비교 제거
- 명시적 boolean mask로 날짜 범위 필터링

Primary source:
- NAVER_FCHART

CSV schema:
date,open,high,low,close,volume,primary_source,crosscheck_source,crosscheck_ok

crosscheck 필드는 비워 두며, 연구 투입 전 별도 검증한다.
