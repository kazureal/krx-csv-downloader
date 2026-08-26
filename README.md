# Korea Raw OHLCV CSV v0.5 diagnostic

v0.4 수집 로직을 그대로 유지하고, OHLC 관계 오류의 원인을 노출하는 진단판.

추가한 것:
- 오류 날짜
- O/H/L/C/V
- 실패 조건
- 필요한 최소 High / 최대 Low
- Naver XML item의 원문 raw_data
- 진단 CSV 다운로드

중요:
- 오류 행 삭제 안 함
- 가격 임의 보정 안 함
- 검증 기준 완화 안 함
- 검증 실패 시 일반 OHLCV CSV 생성 금지
- 연구/백테스트 로직과 분리
