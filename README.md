# Korea OHLCV CSV v1.0.0 — STOCK + INDEX + UNIVERSE

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
