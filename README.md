# Korea OHLCV CSV v0.9.4 KRX DIRECT RAW

## 핵심
- KRX 공식 Data Marketplace JSON endpoint 직접 사용
- 종목코드 -> KRX issue code(ISIN) 검색
- `MDCSTAT01701` 개별종목 시세 추이 호출
- `adjStkPrc=1` = 단순종가/비수정 가격
- 700일 단위 자동 분할
- SK하이닉스 000660의 공식 ISIN `KR7000660001` fallback 포함
- 선택적 KRX 로그인 지원
  - 로그인 정보는 앱 실행 중 KRX 요청에만 사용
  - CSV, 화면 로그, 파일에 ID/PW 저장 안 함
  - 채팅에 ID/PW를 보내지 말 것
- FDR/NAVER/pykrx는 대조·진단용으로 유지
- 자동보정 없음

## SK하이닉스 테스트
1. 종목: SK하이닉스
2. 시작일: 2003-04-14
3. 종료일: 오늘
4. 소스: KRX DIRECT RAW (권장)
5. 먼저 로그인 없이 실행
6. 빈 응답이면 KRX 로그인 영역에서 본인 계정으로 다시 실행
7. 성공 시 CSV를 ChatGPT에 올려 기존 2014-06-09~2026-08-27 파일과 전수대조

HRF OOS는 데이터 QA가 끝나기 전까지 실행하지 않습니다.
