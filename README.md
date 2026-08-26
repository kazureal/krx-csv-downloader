# Korea OHLCV CSV v0.6

원자료 보존형 수집기.

- NAVER FCHART 값을 source_* 필드에 그대로 보존
- OHLC 관계 오류 자동 수정 금지
- verified_* 는 독립 출처 검증 전 비움
- crosscheck_status 기본 NOT_CHECKED
- research_ready 기본 False
- 수정/비수정 가격을 한 열에 혼합하지 않음

v0.6은 '완성된 이중 수집기'가 아니라 안전한 데이터 스키마 단계입니다.
독립 2차 출처가 확정되기 전에는 검증값을 생성하지 않습니다.
