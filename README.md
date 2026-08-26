# Korea Raw OHLCV CSV v0.2

데이터 취득 전용 앱. 연구/백테스트 로직과 분리.

v0.1의 Streamlit Cloud KRX 직접호출 빈 응답 문제 때문에 v0.2는 네이버증권 공개 시세를 primary로 사용한다.
따라서 KRX Raw라고 부르지 않는다.

CSV schema:
date,open,high,low,close,volume,primary_source,crosscheck_source,crosscheck_ok

crosscheck 필드는 아직 비워 두며 연구 투입 전 별도 검증한다.
