# 서울 부동산 시세 (캡스톤 R1-B)
목표: 자치구와 달을 받아 매매 시세를 정리하고 이상 거래 후보를 고른다.
데이터: seoul-realestate MCP의 search_trades 만 쓴다 (서울 열린데이터광장 tbLnOpendataRtmsV). 금액은 만원, 면적은 ㎡.
키: MCP 등록(--env)에만 있다. 파일, 명령, 커밋, 프롬프트에 쓰지 않는다. 호출 대상은 openapi.seoul.go.kr 뿐이다.
  예외(2026-11-17 사용자 승인): homefit 실거래 데이터(고객 데이터 제외)의 S3 백업. 업로드는 사용자가 tools/s3_backup.sh 로 직접 실행한다.
결과물에는 본번, 부번(지번)을 쓰지 않는다. 건물명(단지명)은 HomeFit 아파트 추천에서만 법정동과 함께 쓴다(2026-11-17 사용자 승인). 그 밖의 결과물은 법정동까지만 적는다.
역할: price-summarizer(시세 정리), anomaly-checker(이상 거래 검토, 읽기 전용).
가드레일: .claude/hooks 의 key-guard.sh, net-guard.sh 가 키 노출과 다른 호출 대상을 막는다. 우회하지 않는다.
