"""세션에 저장된 search_trades 원본 결과 파일을 (구, 계약연월)에 대응시킨다. 단지명 등 내용은 출력하지 않는다.

사용: python3 -I tools/map_raw.py <원본 폴더> [<원본 폴더> ...]
출력(JSON): {"map": {"구_YYYYMM": 원본경로}, "ambiguous": [...], "unreadable": n}
한 결과 파일의 모든 행은 같은 구·같은 계약연월이어야 한다(도구가 그렇게 거른다). 같은 조합 파일이 여럿이면 가장 최근 파일을 쓴다.
"""
import json, sys
from collections import Counter
from pathlib import Path

out, amb, bad = {}, [], 0
files = sorted((p for d in sys.argv[1:] for p in Path(d).glob("mcp-seoul-realestate-search_trades-*.txt")), key=lambda p: p.stat().st_mtime)
for p in files:
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))["result"]
    except Exception:
        bad += 1
        continue
    if not rows:
        continue
    keys = Counter((r.get("CGG_NM"), str(r.get("CTRT_DAY", ""))[:6]) for r in rows)
    if len(keys) != 1:
        amb.append({"file": p.name, "keys": len(keys)})
        continue
    (gu, ym), = keys
    out[f"{gu}_{ym}"] = str(p)          # 최근 파일이 덮어씀
print(json.dumps({"map": out, "ambiguous": amb, "unreadable": bad}, ensure_ascii=False))
