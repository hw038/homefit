# HomeFit 서울 — 마이데이터 × 서울 실거래 내 집 찾기 (캡스톤)

가명 마이데이터 고객의 자산·부채·소득으로 **실제로 살 수 있는 예산**을 계산하고, 서울 열린데이터광장 실거래로 **예산 안의 동네와 아파트 단지**를 추천하는 웹 MVP입니다. 좋아요와 인기 급상승은 Valkey로 실시간 공유합니다.

## 폴더
| 경로 | 내용 |
|---|---|
| `homefit/` | 웹 앱 (서버·계산·화면·도구·테스트·문서). 자세한 실행법은 [`homefit/README.md`](homefit/README.md) |
| `homefit/docs/capstone-intro.md` | 기능 소개·데모 시나리오·한계 (발표·안내문용) |
| `homefit/docs/research/` | 규제·고객 데이터·점수 방법론·UX 조사와 개선 로드맵 |
| `seoul-realestate/` | 실거래 조회 MCP 서버 (`search_trades`) |
| `.claude/agents/`, `.claude/hooks/` | 시세 정리·이상거래 검토 에이전트, 키 노출·외부 호출 차단 훅 |
| `CLAUDE.md` | 프로젝트 규칙 |

## 빠른 시작
```bash
# 1) 실거래 MCP 등록 (키는 MCP 등록에만 둔다)
claude mcp add seoul-realestate --env SEOUL_API_KEY=<서울 열린데이터광장 키> -- python3 seoul-realestate/server.py

# 2) 데이터 수집: search_trades 결과 파일을 비식별 저장 (지번은 버리고, 단지명은 HOMEFIT_KEEP_NAME=1 일 때만 보관)
cd homefit
HOMEFIT_KEEP_NAME=1 python3 -I tools/ingest.py <구> <YYYYMM> <search_trades 결과 파일>

# 3) 좋아요 저장소(선택)
valkey-server --bind 127.0.0.1 --port 6379 --dir data/valkey --appendonly yes

# 4) 실행 · 테스트
python3 -I server.py --port 8765          # 127.0.0.1 전용(개발)
python3 -I tests/test_app.py
```
- 가명 고객 데이터는 저장소에 없습니다. `homefit/budget.py` 는 `../../mydata/pseudo/*.csv` (customers, bank_accounts, bank_transactions, card_transactions, securities, pensions, insurance, loans)를 읽습니다.
- 실거래 데이터(`homefit/data/trades/`)와 비밀 값은 `.gitignore` 로 제외했습니다. 위 2)로 다시 만들 수 있습니다.

## 공개 배포 (선택)
- CloudFront → EC2:8765. 보안 그룹은 CloudFront origin-facing prefix list 만 허용하고, 서버는 `X-HF-Origin` 비밀 헤더(`homefit/data/.origin_secret`, 직접 생성)가 없는 외부 요청을 403으로 거절합니다. 공개 경로에서는 데모·서울 거주 고객만 조회되고 요청 수가 제한됩니다.
- 상시 실행: `homefit/deploy/install.sh` (systemd 서비스 `homefit`, `homefit-valkey` 등록)
- S3 백업: `homefit/deploy/.env.local` 에 `HOMEFIT_S3_BUCKET=` 을 적고 `bash homefit/tools/s3_backup.sh --dryrun`

## 데이터 출처와 주의
- 서울 열린데이터광장 「서울시 부동산 실거래가 정보」. 결과는 실거래 통계이며 매물·투자 권유가 아닙니다.
- 대출·세금 규칙(`homefit/policy.json`)은 2026년 6월까지 발표된 정책 기준입니다. 실제 적용 전 확인이 필요합니다.
