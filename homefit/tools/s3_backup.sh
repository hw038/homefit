#!/bin/bash
# HomeFit 실거래 데이터 S3 백업 (사용자가 직접 실행)
#  - 올리는 것: data/trades (지번 삭제본, 단지명 포함), 수집·검증 기록, 정책 히스토리, 매니페스트
#  - 올리지 않는 것: 가명 고객 데이터(mydata), 코드, 키
#  - 버킷은 버전 관리가 켜져 있어 덮어써도 이전 판이 남는다. snapshots/ 에는 날짜별 매니페스트를 따로 둔다.
# 사용: bash tools/s3_backup.sh            (실제 업로드)
#       bash tools/s3_backup.sh --dryrun   (무엇이 올라갈지만 보기)
set -euo pipefail
# 버킷 이름은 공개 저장소에 두지 않는다: 환경 변수나 deploy/.env.local(깃 제외)에서 읽는다
[ -f "$(dirname "$0")/../deploy/.env.local" ] && . "$(dirname "$0")/../deploy/.env.local"
BUCKET="${HOMEFIT_S3_BUCKET:?HOMEFIT_S3_BUCKET 을 지정하세요 (예: deploy/.env.local 에 HOMEFIT_S3_BUCKET=버킷이름)}"
REGION="${HOMEFIT_S3_REGION:-us-east-1}"
cd "$(dirname "$0")/.."

# 1) 올리기 전 검사: 금지 필드가 남은 파일이 있으면 중단
if grep -lE '"(MNO|SNO)"' data/trades/*.json >/dev/null 2>&1; then
  echo "중단: 본번·부번(지번)이 남은 파일이 있습니다" >&2; exit 1
fi

# 2) 매니페스트(파일별 행 수·sha256) 생성
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
python3 -I - "$STAMP" > data/manifest.json <<'PY'
import hashlib, json, sys, pathlib
files = []
for p in sorted(pathlib.Path("data/trades").glob("*.json")):
    b = p.read_bytes()
    files.append({"file": p.name, "rows": len(json.loads(b)), "sha256": hashlib.sha256(b).hexdigest()})
print(json.dumps({"created_utc": sys.argv[1], "files": files, "total_rows": sum(f["rows"] for f in files)}, ensure_ascii=False, indent=1))
PY
echo "매니페스트: $(python3 -I -c "import json;d=json.load(open('data/manifest.json'));print(len(d['files']),'개 파일,',d['total_rows'],'행')")"

DRY=""; [ "${1:-}" = "--dryrun" ] && DRY="--dryrun"
# 3) 업로드
aws s3 sync data/trades "s3://$BUCKET/trades/" --region "$REGION" --exact-timestamps $DRY
for f in manifest.json verification.json policy_history.json; do
  [ -f "data/$f" ] && aws s3 cp "data/$f" "s3://$BUCKET/meta/$f" --region "$REGION" $DRY
done
aws s3 cp data/manifest.json "s3://$BUCKET/snapshots/$STAMP/manifest.json" --region "$REGION" $DRY
echo "완료: s3://$BUCKET  (스냅샷 $STAMP)"
