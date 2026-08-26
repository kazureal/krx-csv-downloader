# KRX CSV Downloader v0.1

목적: 한국 주식 종목명/코드와 기간을 입력해 KRX 비수정(Raw) OHLCV CSV를 만드는 데이터 취득 전용 앱.

## 실행
1. Python 설치 환경에서 `pip install -r requirements.txt`
2. `streamlit run app.py`
3. 아이폰 Safari에서 배포 URL을 열고 홈 화면에 추가 가능.

## 연구 격리
이 앱은 Historical Relevance Filter의 N/k/h, Family A-D, VALID_SESSION_RULE을 실행하거나 수정하지 않는다.
원자료를 다운로드하는 기능만 담당한다.

## 데이터 정책
- pykrx `adjusted=False`
- 장기 기간은 앱 내부에서 약 700일 단위 자동 분할 후 병합
- 실패 시 NAVER/Yahoo 등으로 자동 대체하지 않음
- CSV에 Raw rows를 그대로 보존
