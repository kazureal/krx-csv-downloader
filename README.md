# Korea OHLCV CSV v0.9.1 RAW DIAG

v0.9의 연구용 RAW 수집기를 유지하면서 KRX 수집 진단을 강화한 버전입니다.

## 변경점
- `SK하이닉스`, `하이닉스`, `하이닉스반도체`, `에스케이하이닉스` → `000660` 인식 유지
- KRX `adjusted=False` 연도 단위 분할 수집 유지
- 각 연도별 `RAW_OK / RAW_EMPTY_ADJ_EXISTS / RAW_ERROR` 표시
- pykrx 버전 표시
- 오류 메시지/빈 응답 여부를 화면에서 바로 확인
- `adjusted=True`는 오직 "그 기간 데이터가 존재하는지" 진단 probe에만 사용
- 최종 CSV에는 절대 adjusted=True 데이터를 사용하지 않음
- 원본 보존 / 자동보정 없음 / valid_session 표시 유지

## SK하이닉스 테스트
종목: SK하이닉스
시작일: 2003-04-14
종료일: 오늘
소스: KRX RAW (연구용 권장)

데이터가 없더라도 'KRX 수집 진단 로그' 화면을 캡처하면 원인을 바로 구분할 수 있습니다.
