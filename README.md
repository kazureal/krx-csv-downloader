# Korea OHLCV CSV v0.9 RAW

연구용 한국주식 일봉 OHLCV 수집기.

## v0.9 변경점
- `SK하이닉스`, `하이닉스`, `하이닉스반도체`, `에스케이하이닉스` → `000660` 직접 해석
- KRX 비수정(`adjusted=False`) OHLCV를 연구용 기본 소스로 추가
- 긴 기간은 연도 단위로 분할 수집
- NAVER FCHART는 보조/대조용으로 유지
- 요청 시작일보다 실제 수집 시작일이 늦으면 coverage 경고
- 원본 자동보정 없음
- `Volume==0` 또는 유효하지 않은 OHLC를 `valid_session=False`로 표시하고 원본 행은 보존

## HRF 연구 사용 시
MASTER FREEZE의 연구 규칙을 변경하지 않는다. SK하이닉스 OOS에는 corporate-action boundary 이후의 Raw OHLCV만 사용하고, 데이터 완전성 검증 전 OOS 결과를 열지 않는다.
