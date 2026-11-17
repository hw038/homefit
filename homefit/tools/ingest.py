"""search_trades 결과(JSON {"result":[...]})를 비식별화해 data/trades/{gu}_{ym}.json 으로 저장한다.

사용: python3 -I tools/ingest.py <gu> <YYYYMM> <result_file | ->
- 본번(MNO), 부번(SNO), 지번구분은 저장하지 않는다. 건물명(단지명)은 HOMEFIT_KEEP_NAME=1 일 때만 저장한다.
- 같은 단지 비교/중복 판정용으로 단지 해시(cid)만 남긴다 (화면/API에는 내보내지 않음).
"""
import hashlib, json, sys
from pathlib import Path

import os
OUT = Path(os.environ.get("HOMEFIT_OUT") or Path(__file__).resolve().parent.parent / "data" / "trades")
KEEP_NAME = os.environ.get("HOMEFIT_KEEP_NAME") == "1"
KEEP = ["CGG_NM", "STDG_CD", "STDG_NM", "CTRT_DAY", "THING_AMT", "ARCH_AREA", "LAND_AREA",
        "FLR", "RGHT_SE", "RTRCN_DAY", "ARCH_YR", "BLDG_USG", "DCLR_SE"]

def main():
    gu, ym, src = sys.argv[1], sys.argv[2], sys.argv[3]
    text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    data = json.loads(text)
    rows = data["result"] if isinstance(data, dict) else data
    out, bad = [], 0
    for r in rows:
        if r.get("CGG_NM") != gu or not str(r.get("CTRT_DAY", "")).startswith(ym):
            bad += 1
            continue
        cid = hashlib.sha256("|".join(str(r.get(k, "")) for k in
              ("CGG_NM", "STDG_CD", "MNO", "SNO", "BLDG_NM")).encode()).hexdigest()[:12]
        row = {k: r.get(k) for k in KEEP}
        if KEEP_NAME:                       # 단지명 보존(본번·부번은 여전히 버림)
            row["BLDG_NM"] = (r.get("BLDG_NM") or "").strip()
        row["THING_AMT"] = int(str(row["THING_AMT"]).replace(",", "") or 0)
        row["cid"] = cid
        out.append(row)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{gu}_{ym}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    cancelled = sum(1 for r in out if r["RTRCN_DAY"])
    print(json.dumps({"gu": gu, "ym": ym, "rows_in": len(rows), "saved": len(out),
                      "mismatch_dropped": bad, "cancelled": cancelled,
                      "hit_cap_1000": len(rows) >= 1000}, ensure_ascii=False))

if __name__ == "__main__":
    main()
