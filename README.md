# Korea OHLCV CSV v0.9.2 SOURCE TEST

목적: SK하이닉스 000660의 2003-04-14 이후 장기 OHLCV 확보 가능성을 소스별로 진단합니다.

- KRX RAW: 기존 pykrx adjusted=False 진단 유지
- FDR 장기이력 후보: FinanceDataReader 국내 종목 경로. 공식 프로젝트 문서는 000660 전체(1999~현재) 조회 예시를 제공함.
- NAVER FCHART: 기존 보조/대조 경로 유지
- FDR 결과는 자동으로 HRF 연구용 RAW로 승인하지 않음
- CSV에 research_source_status=CANDIDATE_NOT_YET_APPROVED 표시
- 원본 자동보정 없음

권장 테스트:
1. 종목: SK하이닉스
2. 시작: 2003-04-14
3. 종료: 오늘
4. 소스: FDR 장기이력 후보 (대조 후 승인)
5. 실제 시작일/행 수 확인 후 CSV 다운로드
6. 그 CSV를 ChatGPT에 올려 기존 2014-06-09~2026-08-27 파일과 OHLCV 전행 overlap 검증
