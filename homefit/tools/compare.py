"""두 비식별 파일(ingest 산출물)이 같은 거래 집합인지 비교한다. 사용: python3 -I tools/compare.py A.json B.json [--ignore-name]
출력: {"equal": bool, "n_a", "n_b", "only_a", "only_b"} (차이 행은 개수만; 내용은 cid 없이 필드 일부만)"""
import json, sys
from collections import Counter
def key(r): return json.dumps({k: r.get(k) for k in sorted(r)}, ensure_ascii=False, sort_keys=True)
args = [x for x in sys.argv[1:] if not x.startswith("--")]
IGNORE_NAME = "--ignore-name" in sys.argv          # 단지명 있는 파일 vs 없는 파일 비교(나머지 필드·단지해시 일치 확인)
a, b = (json.load(open(p, encoding="utf-8")) for p in args[:2])
if IGNORE_NAME:
    a = [{k: v for k, v in r.items() if k != "BLDG_NM"} for r in a]
    b = [{k: v for k, v in r.items() if k != "BLDG_NM"} for r in b]
ca, cb = Counter(map(key, a)), Counter(map(key, b))
oa, ob = ca - cb, cb - ca
def brief(c): return [{k: json.loads(x)[k] for k in ("STDG_NM", "CTRT_DAY", "THING_AMT", "ARCH_AREA", "FLR")} for x in list(c.elements())[:5]]
print(json.dumps({"equal": not oa and not ob, "n_a": len(a), "n_b": len(b), "only_a": sum(oa.values()), "only_b": sum(ob.values()),
                  "sample_only_a": brief(oa), "sample_only_b": brief(ob)}, ensure_ascii=False))
