# Korea Raw OHLCV CSV v0.4

데이터 취득 전용 앱. 연구/백테스트 로직과 분리.

## v0.4 fix
v0.3에서 발생한:
`ValueError: multi-byte encodings are not supported`
를 수정.

원인:
Naver legacy chart XML의 EUC-KR/CP949 인코딩 선언을 Python 3.14 XML parser가 직접 처리하지 못하는 경우가 있음.

수정:
1. HTTP bytes를 EUC-KR -> CP949 -> UTF-8 순서로 직접 decode
2. XML declaration 제거
3. Unicode 문자열을 ElementTree로 parse

Primary source:
NAVER_FCHART

CSV schema:
date,open,high,low,close,volume,primary_source,crosscheck_source,crosscheck_ok
