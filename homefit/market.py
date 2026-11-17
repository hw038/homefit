"""실거래(비식별) 집계와 '상승 여력' 점수 — docs/research/methodology.md 구현.

입력: data/trades/{구}_{YYYYMM}.json (tools/ingest.py 산출물; 본번·부번·건물명 없음).
외부로 내보내는 값은 구·법정동·면적대·집계값까지다. cid(단지 해시), 층, 계약일, 개별 거래는 내보내지 않는다.
셀(구×법정동×면적대) 안의 서로 다른 단지가 2곳 미만이면 시세를 공개하지 않는다.
"""
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "trades"

P0 = ("202508", "202509", "202510")          # 전년 기준
P1 = ("202608", "202609", "202610")          # 비교 대상
P0V = ("202508", "202509")                    # 거래량 비교 (신고 완결 월)
P1V = ("202608", "202609")
PN = ("202611",)                              # 잠정
LATE = ("202610",) + PN                       # 단기 모멘텀 후반부
R = P1 + PN                                   # 최신 시세 창
ALL_MONTHS = P0 + P1 + PN                     # 점수 계산에 쓰는 달
TREND_MONTHS = tuple(f"{y}{m:02d}" for y, m in [(2025, m) for m in range(8, 13)] + [(2026, m) for m in range(1, 12)])  # 추이 표시(16개월)
BANDS = {"S": "60㎡ 이하", "M": "60~85㎡", "L": "85㎡ 초과"}
REP_AREA = {"S": 49.0, "M": 84.0, "L": 114.0}
BASE_YEAR = 2026

MIN_CELL_N, GOOD_CELL_N, MIN_CELL_CID = 5, 10, 2
MIN_YOY_SIDE_N, MIN_MATCH_UNITS, MIN_MATCH_TRADES = 8, 3, 6
MIN_VOL_BASE, SHRINK_K, ALPHA = 5, 10, 10
DIRECT_LOW, OUTLIER_Z = 0.70, 3.5
LOW_SAMPLE = 15

DIRECTION = {"disc": 1, "yoy": 1, "catch_up": 1, "vol": 1, "mom": 1, "liquidity": 1,
             "age": -1, "disp": -1, "cancel": -1, "direct": -1, "budget_fit": 1}
LABELS = {"disc": "같은 구보다 저렴한 정도", "yoy": "1년 전보다 오른 정도", "catch_up": "구 평균보다 덜 올라 따라잡을 여지",
          "vol": "거래량 회복", "mom": "최근 한 달 흐름(잠정)", "liquidity": "거래가 활발한 정도", "age": "건물이 새것인 정도",
          "disp": "가격이 고른 정도", "cancel": "계약 취소가 적은 정도", "direct": "중개사를 통한 거래 비중",
          "budget_fit": "내 예산에 맞는 정도"}


# ---------- 공통 ----------

def band_of(area):
    return "S" if area <= 60 else ("M" if area <= 85 else "L")


def age_cls(age):
    if age is None:
        return None
    return "N" if age <= 5 else "S" if age <= 15 else "O" if age <= 29 else "R"


def flr_cls(f):
    try:
        f = int(f)
    except (TypeError, ValueError):
        return "mid"
    return "low" if f <= 3 else "mid" if f <= 14 else "high"


def quantile(values, p):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def wmedian(pairs):
    """[(value, weight)] 가중 중앙값."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def _d(s):
    s = str(s)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def percentile_rank(values, x):
    vals = [v for v in values if v is not None]
    if x is None or not vals:
        return None
    below = sum(v < x for v in vals)
    equal = sum(v == x for v in vals)
    return 100.0 * (below + 0.5 * equal) / len(vals)


# ---------- 적재 ----------

def load_rows(data_dir=DATA_DIR):
    rows, coverage = [], defaultdict(dict)
    for path in sorted(Path(data_dir).glob("*_*.json")):
        gu, ym = path.stem.rsplit("_", 1)
        part = json.loads(path.read_text(encoding="utf-8"))
        coverage[gu][ym] = len(part)
        for r in part:
            r = dict(r)
            r["ym"] = str(r.get("CTRT_DAY", ""))[:6]
            rows.append(r)
    return rows, {g: dict(v) for g, v in coverage.items()}


# ---------- 정제 ----------

def _win(ym):
    return "P0" if ym in P0 else ("R" if ym in R else "G")


def clean(rows):
    """아파트 행을 정제한다. 반환: valid, cancel_pool, excluded(사유→건수), excluded_by_gu(구→사유→건수), stats"""
    excluded = Counter()
    by_gu = defaultdict(Counter)

    def drop(r, reason):
        excluded[reason] += 1
        by_gu[r.get("CGG_NM")][reason] += 1

    apt = [r for r in rows if r.get("BLDG_USG") == "아파트"]
    total = len(apt)
    seen, base, cancel_pool = set(), [], []
    for r in apt:
        try:
            amt = int(r.get("THING_AMT") or 0)
            area = float(r.get("ARCH_AREA") or 0)
        except (TypeError, ValueError):
            amt, area = 0, 0
        if amt <= 0 or area < 10 or area > 300:
            drop(r, "면적이나 금액 정보가 빠진 기록")
            continue
        key = (r.get("cid"), r.get("CTRT_DAY"), amt, area, r.get("FLR"), r.get("DCLR_SE"), r.get("RTRCN_DAY") or "")
        if key in seen:
            drop(r, "같은 거래가 두 번 들어간 기록")
            continue
        seen.add(key)
        yr = str(r.get("ARCH_YR") or "")
        age = (int(str(r["CTRT_DAY"])[:4]) - int(yr)) if yr.isdigit() and int(yr) > 1900 else None
        t = {"gu": r["CGG_NM"], "dong_cd": r.get("STDG_CD"), "dong": r.get("STDG_NM"), "ym": r["ym"],
             "day": str(r["CTRT_DAY"]), "amt": amt, "area": area, "ppm": amt / area, "band": band_of(area),
             "atype": round(area), "cid": r.get("cid"), "flr": r.get("FLR"), "age": age,
             "age_cls": age_cls(age), "flr_cls": flr_cls(r.get("FLR")), "direct": r.get("DCLR_SE") == "직거래",
             "apt": (r.get("BLDG_NM") or "").strip() or None, "built": int(yr) if yr.isdigit() and int(yr) > 1900 else None}
        if r.get("RTRCN_DAY"):
            cancel_pool.append(t)
            drop(r, "계약이 취소된 거래")
            continue
        base.append(t)

    # 참고: 해제 후 재거래·90일 반복거래 규칙은 쓰지 않는다. 비식별 데이터에는 동·호가 없어(cid 는 단지 단위)
    # 같은 단지·같은 층·같은 평형의 서로 다른 집을 같은 집으로 잘못 묶기 때문이다(시험 시 아파트 거래의 11% 오탐).
    stage = base

    # (f) 직거래 저가: 기준 단가 계층
    brokered = [t for t in stage if not t["direct"]]
    by_unit = defaultdict(list)
    by_cell_age = defaultdict(list)
    by_cell = defaultdict(list)
    for t in brokered:
        by_unit[(t["cid"], t["atype"])].append(t)
        win = _win(t["ym"])
        by_cell_age[(t["gu"], t["dong_cd"], t["band"], t["age_cls"], win)].append(t["ppm"])
        by_cell[(t["gu"], t["dong_cd"], t["band"])].append(t["ppm"])

    def ref_ppm(t):
        near = [u["ppm"] for u in by_unit.get((t["cid"], t["atype"]), []) if abs((_d(u["day"]) - _d(t["day"])).days) <= 180]
        if len(near) >= 3:
            return statistics.median(near)
        win = _win(t["ym"])
        v = by_cell_age.get((t["gu"], t["dong_cd"], t["band"], t["age_cls"], win), [])
        if len(v) >= 5:
            return statistics.median(v)
        v = by_cell.get((t["gu"], t["dong_cd"], t["band"]), [])
        if len(v) >= 5:
            return statistics.median(v)
        return None

    stage2 = []
    for t in stage:
        if t["direct"]:
            ref = ref_ppm(t)
            if ref and t["ppm"] < DIRECT_LOW * ref:
                drop(t, "시세보다 크게 싼 직거래(시세의 70% 미만)")
                continue
        stage2.append(t)

    # (g) 극단값: 셀×창(R 또는 P0) 안에서 로그 단가 robust z
    groups = defaultdict(list)
    for t in stage2:
        win = _win(t["ym"])
        groups[(t["gu"], t["dong_cd"], t["band"], win)].append(t)
    valid = []
    for g, ts in groups.items():
        if len(ts) >= 8:
            logs = [math.log(t["ppm"]) for t in ts]
            med = statistics.median(logs)
            mad = statistics.median(abs(x - med) for x in logs)
            if mad > 0:
                for t, x in zip(ts, logs):
                    if abs(x - med) / (1.4826 * mad) > OUTLIER_Z:
                        drop(t, "주변 시세와 동떨어진 가격")
                    else:
                        valid.append(t)
                continue
        valid.extend(ts)
    stats = {"apt_rows": total, "valid": len(valid), "cancelled": len(cancel_pool)}
    return valid, cancel_pool, dict(excluded), {g: dict(c) for g, c in by_gu.items()}, stats


# ---------- 보정 계수 ----------

def _factors(trades, attr, base_cls, classes):
    """구×band → {cls: factor}. 셀별 (cls 중앙값 / 기준 중앙값) 로그비율의 가중 중앙값."""
    cells = defaultdict(lambda: defaultdict(list))
    for t in trades:
        if t[attr] is not None:
            cells[(t["gu"], t["dong_cd"], t["band"])][t[attr]].append(t["ppm"])
    lr = defaultdict(list)          # (gu, band, cls) → [(lr, w)]
    lr_seoul = defaultdict(list)    # (band, cls)
    for (gu, _dong, band), by in cells.items():
        b = by.get(base_cls, [])
        if len(b) < 3:
            continue
        mb = statistics.median(b)
        for cls in classes:
            v = by.get(cls, [])
            if len(v) >= 3:
                x = (math.log(statistics.median(v) / mb), min(len(v), len(b)))
                lr[(gu, band, cls)].append(x)
                lr_seoul[(band, cls)].append(x)
    seoul = {}
    for (band, cls), xs in lr_seoul.items():
        if len(xs) >= 3:
            seoul[(band, cls)] = math.exp(wmedian(xs))
    out = {}
    for (gu, band, cls), xs in lr.items():
        if len(xs) >= 3:
            out[(gu, band, cls)] = math.exp(wmedian(xs))

    def f(gu, band, cls):
        if cls is None or cls == base_cls:
            return 1.0
        return out.get((gu, band, cls)) or seoul.get((band, cls)) or 1.0
    return f


def adjust(valid):
    fa = _factors(valid, "age_cls", "O", ("N", "S", "R"))
    ff = _factors(valid, "flr_cls", "mid", ("low", "high"))
    for t in valid:
        t["ppm_adj"] = t["ppm"] / (fa(t["gu"], t["band"], t["age_cls"]) * ff(t["gu"], t["band"], t["flr_cls"]))
    return valid


# ---------- 지표 ----------

def _matched(trades_a, trades_b):
    """(cid, atype±1) 매칭 로그비율. a=이전, b=이후. 반환 (yoy, units, n_a, n_b)"""
    ua, ub = defaultdict(list), defaultdict(list)
    for t in trades_a:
        ua[(t["cid"], t["atype"])].append(t["ppm"])
    for t in trades_b:
        ub[(t["cid"], t["atype"])].append(t["ppm"])
    pairs, na, nb = [], 0, 0
    used_a, matched = set(), {}
    for kb in ub:                                   # 1) 정확히 같은 (단지, 평형) 먼저
        if kb in ua:
            matched[kb] = kb
            used_a.add(kb)
    for (cid, at) in ub:                            # 2) 남은 것만 ±1㎡ 허용
        if (cid, at) in matched:
            continue
        ka = next((k for k in ((cid, at - 1), (cid, at + 1)) if k in ua and k not in used_a), None)
        if ka is not None:
            matched[(cid, at)] = ka
            used_a.add(ka)
    for kb, ka in matched.items():
        va, vb = ua[ka], ub[kb]
        pairs.append((math.log(statistics.median(vb) / statistics.median(va)), min(len(va), len(vb))))
        na += len(va)
        nb += len(vb)
    if len(pairs) >= MIN_MATCH_UNITS and na >= MIN_MATCH_TRADES and nb >= MIN_MATCH_TRADES:
        return math.exp(wmedian(pairs)) - 1, len(pairs), na, nb
    return None, len(pairs), na, nb


def _yoy(p0, p1):
    """λ 결합 전년비. 반환 (yoy, method, units)"""
    a = None
    if len(p0) >= MIN_YOY_SIDE_N and len(p1) >= MIN_YOY_SIDE_N:
        a = statistics.median(t["ppm_adj"] for t in p1) / statistics.median(t["ppm_adj"] for t in p0) - 1
    b, units, _, _ = _matched(p0, p1)
    if b is not None:
        lam = units / (units + 3)
        return (lam * b + (1 - lam) * a if a is not None else b), "matched", units
    if a is not None:
        return a, "median_adj", units
    return None, None, units


def monthly_trend(ts, state=lambda ym: "ok", min_n=MIN_CELL_N, min_cid=MIN_CELL_CID):
    """월별 ㎡당 중앙값(TREND_MONTHS). 거래 min_n 미만이거나 단지 min_cid 미만인 달은 값을 숨긴다(단지 특정 방지).
    state(ym): 'ok' | 'missing'(수집 안 함) | 'partial'(일부 누락 가능)"""
    out = []
    for ym in TREND_MONTHS:
        st = state(ym)
        v = [t for t in ts if t["ym"] == ym]
        ok = st not in ("missing", "unavailable") and len(v) >= min_n and len({t["cid"] for t in v}) >= min_cid
        out.append({"ym": ym, "ppm_med": round(statistics.median(t["ppm"] for t in v), 1) if ok else None,
                    "n": len(v) if ok else None, "hidden": st not in ("missing", "unavailable") and bool(v) and not ok,
                    "state": st, "provisional": ym in PN})
    return out


def period_change(ts):
    """기간 중앙값(원 단가)과 단순 변화율. 각 기간 표본 MIN_CELL_N 이상일 때만."""
    def med(months):
        v = [t["ppm"] for t in ts if t["ym"] in months]
        return (statistics.median(v), len(v)) if len(v) >= MIN_CELL_N and len({t["cid"] for t in ts if t["ym"] in months}) >= MIN_CELL_CID else (None, len(v))
    (m0, n0), (m1, n1), (mn, nn) = med(P0), med(P1), med(PN)
    return {"p0": {"ppm_med": _r(m0, 1), "n": n0}, "p1": {"ppm_med": _r(m1, 1), "n": n1}, "pn": {"ppm_med": _r(mn, 1), "n": nn},
            "yoy_simple": _r(m1 / m0 - 1) if (m0 and m1) else None}


APT_MIN_N = 2          # 단지를 보여 줄 최근 거래 수 하한(1건이면 그 거래 가격이 곧 시세가 되어 믿기 어려움)
APT_WEIGHTS = {"live": {"disc": 0.30, "new": 0.25, "liq": 0.25, "yoy": 0.20},
               "invest": {"yoy": 0.40, "disc": 0.30, "liq": 0.20, "new": 0.10}}
APT_LABELS = {"disc": "같은 동네보다 저렴함", "new": "건물이 새것", "liq": "거래가 활발함", "yoy": "1년 동안 오름"}


_LOT = re.compile(r"\(\s*\d+(?:\s*-\s*\d+)?\s*\)|^\s*\d+(?:\s*-\s*\d+)?\s*$")


def clean_name(n):
    """단지명에서 지번(본번-부번) 형태를 지운다. 남는 이름이 없으면 None (예: '(50-5)' → None, '○○빌(891-6)' → '○○빌')."""
    if not n:
        return None
    t = re.sub(r"\s+", " ", _LOT.sub("", n)).strip(" -·,")
    return t if t and not re.fullmatch(r"[\d\s\-()]+", t) else None


MICRO_AREA = 30.0      # 전용 30㎡ 미만은 원룸형(기본으로 단지 추천에서 뺀다)
BULK_DAY = 5           # 같은 단지·같은 날 계약이 이만큼 몰리면 분양·일괄 계약일 수 있음


def _surge(ts, prev_ok):
    """최근 3개월 vs 직전 3개월 '거래가 있었던 날짜 수'(같은 날 몰린 계약은 1로)."""
    if not prev_ok:
        return None
    d1 = len({t["day"] for t in ts if t["ym"] in SURGE_RECENT})
    d0 = len({t["day"] for t in ts if t["ym"] in SURGE_PREV})
    return {"recent_days": d1, "prev_days": d0, "ratio": round((d1 + 1) / (d0 + 1), 2)}


def _apt_yoy(p0, p1):
    """같은 단지 안에서 같은 평형(±1㎡)끼리 1년 전과 비교한 ㎡당 가격 변화(가중 중앙값)."""
    a, b = defaultdict(list), defaultdict(list)
    for t in p0:
        a[t["atype"]].append(t["ppm"])
    for t in p1:
        b[t["atype"]].append(t["ppm"])
    pairs = []
    for at, vb in b.items():
        k = next((x for x in (at, at - 1, at + 1) if x in a), None)
        if k is not None:
            pairs.append((math.log(statistics.median(vb) / statistics.median(a[k])), min(len(vb), len(a[k]))))
    if not pairs or sum(w for _, w in pairs) < 2:
        return None
    return math.exp(wmedian(pairs)) - 1


SURGE_RECENT = ("202609", "202610", "202611")
SURGE_PREV = ("202606", "202607", "202608")


def complexes_of(ts, rr, cell_id, cell_adj_med, all_ts=(), state=lambda ym: "ok"):
    """셀(동·면적대) 안 단지별 통계. 최근 거래 APT_MIN_N 건 이상인 단지만."""
    by_r, by_all, by_any = defaultdict(list), defaultdict(list), defaultdict(list)
    for t in all_ts:
        by_any[t["cid"]].append(t)
    prev_ok = all(state(m) not in ("missing", "unavailable") for m in SURGE_PREV)
    for t in rr:
        by_r[t["cid"]].append(t)
    for t in ts:
        by_all[t["cid"]].append(t)
    out = []
    for cid, v in by_r.items():
        if len(v) < APT_MIN_N:
            continue
        names = Counter(clean_name(t["apt"]) for t in v if clean_name(t["apt"]))
        days = Counter(t["day"] for t in v)
        built = Counter(t["built"] for t in v if t["built"])
        amt = [t["amt"] for t in v]
        allv = by_all[cid]
        yoy = _apt_yoy([t for t in allv if t["ym"] in P0], [t for t in allv if t["ym"] in P1])
        adj = statistics.median(t["ppm_adj"] for t in v)
        last = max(t["day"] for t in v)
        out.append({
            "apt_id": cid[:10], "cell_id": cell_id, "apt_name": names.most_common(1)[0][0] if names else None,
            "built": built.most_common(1)[0][0] if built else None,
            "n": len(v), "n_prov": sum(t["ym"] in PN for t in v), "n_days": len(days),
            "bulk": days.most_common(1)[0][1] >= BULK_DAY,
            "area_med": round(statistics.median(t["area"] for t in v), 1),
            "areas": sorted({round(t["area"]) for t in v}),
            "price": {"p25": round(quantile(amt, .25) / 100) * 100, "med": round(statistics.median(amt) / 100) * 100,
                      "p75": round(quantile(amt, .75) / 100) * 100, "min": min(amt), "max": max(amt)},
            "ppm_med": round(statistics.median(t["ppm"] for t in v), 1),
            "yoy": _r(yoy), "disc": _r(1 - adj / cell_adj_med) if cell_adj_med else None,
            "last_ym": last[:6],
            "surge": _surge(by_any[cid], prev_ok),
        })
    return out


def build(rows, coverage=None, capped=(), partial=(), unavailable=()):
    """전체 집계. 반환 dict: cells(공개 셀), suppressed, gus, monthly, exclusions, meta.

    coverage: {구: {YYYYMM: 행수}} — 수집하지 않은 달은 '거래 0건'이 아니라 '자료 없음'으로 본다.
    capped: 1,000건 상한에 닿은 '구_YYYYMM' — 거래량 비교에서 뺀다.
    """
    valid, cancels, excluded, excluded_by_gu, stats = clean(rows)
    capped = set(capped)
    partial = set(partial) | capped
    unavailable = set(unavailable)

    def state_of(gu):
        def st(ym):
            if f"{gu}_{ym}" in unavailable:
                return "unavailable"
            if coverage is not None and ym not in coverage.get(gu, {}):
                return "missing"
            return "partial" if f"{gu}_{ym}" in partial else "ok"
        return st

    def months_ok(gu, months):
        if coverage is None:
            return True
        have = coverage.get(gu, {})
        return all(m in have and f"{gu}_{m}" not in capped for m in months)

    def has(gu, months):
        return coverage is None or any(m in coverage.get(gu, {}) for m in months)
    adjust(valid)

    def period(t):
        return "P0" if t["ym"] in P0 else ("P1" if t["ym"] in P1 else ("PN" if t["ym"] in PN else None))

    cell_tr = defaultdict(list)
    gb_tr = defaultdict(list)
    cell_all = defaultdict(list)                 # 추이 표시용(16개월 전체)
    gb_all = defaultdict(list)
    for t in valid:
        cell_all[(t["gu"], t["dong_cd"], t["band"])].append(t)
        gb_all[(t["gu"], t["band"])].append(t)
        if period(t):
            cell_tr[(t["gu"], t["dong_cd"], t["band"])].append(t)
            gb_tr[(t["gu"], t["band"])].append(t)
    cancel_cell = Counter()
    cancel_gb = Counter()
    for c in cancels:
        if c["ym"] in R:
            cancel_cell[(c["gu"], c["dong_cd"], c["band"])] += 1
            cancel_gb[(c["gu"], c["band"])] += 1

    # 구×면적대 기준값
    gb = {}
    for (gu, band), ts in gb_tr.items():
        p0 = [t for t in ts if t["ym"] in P0]
        p1 = [t for t in ts if t["ym"] in P1]
        rr = [t for t in ts if t["ym"] in R]
        yoy, method, units = _yoy(p0, p1)
        n0 = sum(t["ym"] in P0V for t in ts)
        n1 = sum(t["ym"] in P1V for t in ts)
        n_all = len(rr) + cancel_gb[(gu, band)]
        gb[(gu, band)] = {
            "monthly": monthly_trend(gb_all[(gu, band)], state_of(gu)), "periods": period_change(ts),
            "yoy": yoy, "yoy_method": method,
            "vol": math.log((n1 + 2) / (n0 + 2)) if (n0 >= MIN_VOL_BASE and months_ok(gu, P0V + P1V)) else None,
            "ref_adj": statistics.median(t["ppm_adj"] for t in rr) if rr else None,
            "cancel_rate": cancel_gb[(gu, band)] / n_all if n_all else 0.0,
            "direct_rate": (sum(t["direct"] for t in rr) / len(rr)) if rr else 0.0,
            "n_R": len(rr),
        }

    cells, suppressed = [], Counter()
    for (gu, dcd, band), ts in cell_tr.items():
        rr = [t for t in ts if t["ym"] in R]
        n_R = len(rr)
        cids = Counter(t["cid"] for t in rr)
        if n_R < MIN_CELL_N:
            suppressed["최근 거래가 5건 미만"] += 1
            continue
        if len(cids) < MIN_CELL_CID:
            suppressed["아파트 단지가 1곳뿐"] += 1
            continue
        p0 = [t for t in ts if t["ym"] in P0]
        p1 = [t for t in ts if t["ym"] in P1]
        g = gb[(gu, band)]
        ppm = [t["ppm"] for t in rr]
        med, q1, q3 = statistics.median(ppm), quantile(ppm, .25), quantile(ppm, .75)
        rep = statistics.median(t["area"] for t in rr)
        yoy, ymethod, units = _yoy(p0, p1)
        n_eff = min(len(p0), len(p1))
        g_yoy = g["yoy"]
        if yoy is not None and g_yoy is not None:
            yoy_shr = (n_eff * yoy + SHRINK_K * g_yoy) / (n_eff + SHRINK_K)
        elif yoy is not None:
            yoy_shr = yoy
        else:
            yoy_shr = None                       # 셀 값 없음 → 점수에서 제외(가중치 재배분)
        mom, _, _, _ = _matched([t for t in ts if t["ym"] in P1V], [t for t in ts if t["ym"] in LATE])
        n0 = sum(t["ym"] in P0V for t in ts)
        n1 = sum(t["ym"] in P1V for t in ts)
        vol = (math.log((n1 + 2) / (n0 + 2)) if n0 >= MIN_VOL_BASE else g["vol"]) if months_ok(gu, P0V + P1V) else None
        complete = [t for t in rr if t["ym"] in P1V]
        liquidity = len(complete) / len(P1V) / max(len(cids), 1)
        disc = (1 - statistics.median(t["ppm_adj"] for t in rr) / g["ref_adj"]) if g["ref_adj"] else None
        disc_shr = n_R * disc / (n_R + SHRINK_K) if disc is not None else None
        catch_up = ((g_yoy - yoy_shr) if (disc_shr is not None and disc_shr > 0) else 0.0) if (g_yoy is not None and yoy_shr is not None) else None
        c = cancel_cell[(gu, dcd, band)]
        cancel_rate = (c + ALPHA * g["cancel_rate"]) / (n_R + c + ALPHA)
        direct_rate = (sum(t["direct"] for t in rr) + ALPHA * g["direct_rate"]) / (n_R + ALPHA)
        ages = [t["age"] for t in rr if t["age"] is not None]
        if n_R >= 20 and units >= 5:
            grade = "A"
        elif n_R >= GOOD_CELL_N and (units >= 3 or (len(p0) >= 8 and len(p1) >= 8)):
            grade = "B"
        else:
            grade = "C"
        dominance = cids.most_common(1)[0][1] / n_R
        warnings = []
        if grade == "C":
            warnings.append(f"최근 거래가 {n_R}건(단지 {len(cids)}곳)뿐이라 숫자가 크게 달라질 수 있어요.")
        if dominance > 0.6:
            warnings.append("한 단지의 거래가 대부분이라 동네 전체 시세와는 다를 수 있어요.")
        n_prov = sum(t["ym"] in PN for t in rr)
        if n_prov or any(t["ym"] == "202610" for t in rr):
            warnings.append("2026년 10~11월 거래는 아직 신고가 다 들어오지 않아 나중에 바뀔 수 있어요.")
        dong = Counter(t["dong"] for t in rr).most_common(1)[0][0]
        cells.append({
            "id": f"{gu}|{dcd}|{band}", "gu": gu, "dong": dong, "band": band, "band_label": BANDS[band],
            "rep_area": round(rep, 1),
            "ppm": {"med": round(med, 1), "p25": round(q1, 1), "p75": round(q3, 1)},
            "price": {"p25": round(q1 * rep / 100) * 100, "med": round(med * rep / 100) * 100, "p75": round(q3 * rep / 100) * 100},
            "sample": {"n": n_R, "n_prov": n_prov, "n_prior": len(p0), "n_recent": len(p1), "complexes": len(cids),
                       "low_sample": n_R < LOW_SAMPLE, "match_units": units},
            "grade": grade,
            "metrics": {"yoy": _r(yoy_shr), "yoy_raw": _r(yoy), "yoy_method": ymethod, "disc": _r(disc_shr),
                        "catch_up": _r(catch_up), "vol": _r(vol), "mom": _r(mom), "liquidity": _r(liquidity),
                        "age": statistics.median(ages) if ages else None, "disp": _r((q3 - q1) / med),
                        "cancel": _r(cancel_rate), "direct": _r(direct_rate)},
            "warnings": warnings,
            "trend": {"monthly": monthly_trend(cell_all[(gu, dcd, band)], state_of(gu)), "periods": period_change(ts)},
            "complexes": complexes_of(ts, rr, f"{gu}|{dcd}|{band}", statistics.median(t["ppm_adj"] for t in rr),
                                      cell_all[(gu, dcd, band)], state_of(gu)),
        })

    # 점수용 백분위 (같은 band 비교군, 10개 미만이면 전체)
    by_band = defaultdict(list)
    for c in cells:
        by_band[c["band"]].append(c)
    for c in cells:
        U = by_band[c["band"]] if len(by_band[c["band"]]) >= 10 else cells
        pct = {}
        for k, d in DIRECTION.items():
            if k == "budget_fit":
                continue
            x = c["metrics"].get(k)
            p = percentile_rank([u["metrics"].get(k) for u in U], x)
            pct[k] = None if p is None else (p if d > 0 else 100 - p)
        c["pct"] = pct
        c["peer_n"] = len(U)
    apts_by_band = defaultdict(list)
    for c in cells:
        for a in c["complexes"]:
            a["gu"], a["dong"], a["band"], a["band_label"] = c["gu"], c["dong"], c["band"], c["band_label"]
            apts_by_band[c["band"]].append(a)
    for band, lst in apts_by_band.items():
        vals = {"yoy": [a["yoy"] for a in lst], "disc": [a["disc"] for a in lst], "liq": [a["n_days"] for a in lst],
                "new": [a["built"] for a in lst]}
        for a in lst:
            a["pct"] = {"yoy": percentile_rank(vals["yoy"], a["yoy"]), "disc": percentile_rank(vals["disc"], a["disc"]),
                        "liq": percentile_rank(vals["liq"], a["n_days"]), "new": percentile_rank(vals["new"], a["built"])}

    # 구 요약과 월별 추이 (아파트 전 면적대)
    gus = []
    monthly = defaultdict(list)
    by_gu = defaultdict(list)
    for t in valid:
        by_gu[t["gu"]].append(t)
    for gu, ts in by_gu.items():
        st = state_of(gu)
        for ym in TREND_MONTHS:
            v = [t["ppm"] for t in ts if t["ym"] == ym]
            ok = st(ym) not in ("missing", "unavailable") and len(v) >= MIN_CELL_N        # 공개 기준 미만 월은 값 숨김
            monthly[gu].append({"ym": ym, "ppm_med": round(statistics.median(v), 1) if ok else None,
                                "n": len(v) if ok else None, "collected": st(ym) != "missing", "state": st(ym),
                                "provisional": ym in PN})
        p0 = [t for t in ts if t["ym"] in P0]
        p1 = [t for t in ts if t["ym"] in P1]
        rr = [t for t in ts if t["ym"] in R]
        yoy, method, units = _yoy(p0, p1)
        n0 = sum(t["ym"] in P0V for t in ts)
        n1 = sum(t["ym"] in P1V for t in ts)
        if not has(gu, P1):
            status = "provisional_only" if has(gu, PN) else "no_recent"
        elif not has(gu, P0):
            status = "no_prior"
        else:
            status = "ok"
        if status != "ok":
            yoy, method = None, None
        gus.append({"gu": gu, "ppm_med": round(statistics.median(t["ppm"] for t in rr), 1) if len(rr) >= MIN_CELL_N else None,
                    "ppm_med_prior": round(statistics.median(t["ppm"] for t in p0), 1) if p0 else None,
                    "yoy": _r(yoy), "yoy_method": method, "n_recent": len(p1), "n_prior": len(p0),
                    "n_prov": sum(t["ym"] in PN for t in ts),
                    "vol_chg": _r((n1 + 2) / (n0 + 2) - 1) if (n0 >= MIN_VOL_BASE and months_ok(gu, P0V + P1V)) else None,
                    "status": status,
                    "cells": sum(1 for c in cells if c["gu"] == gu)})
    seoul_monthly = []
    gus_all = sorted(by_gu)
    for ym in TREND_MONTHS:
        v = [t["ppm"] for t in valid if t["ym"] == ym]
        miss = [g for g in gus_all if state_of(g)(ym) in ("missing", "unavailable")]
        part = [g for g in gus_all if state_of(g)(ym) == "partial"]
        seoul_monthly.append({"ym": ym, "n": len(v) if len(miss) < len(gus_all) else None,
                              "ppm_med": round(statistics.median(v), 1) if v else None, "provisional": ym in PN,
                              "missing_gus": len(miss), "partial_gus": part})
    gu_band = {f"{g}|{b}": {"monthly": v["monthly"], "periods": v["periods"], "yoy": _r(v["yoy"]), "yoy_method": v["yoy_method"]}
               for (g, b), v in gb.items()}
    return {"cells": cells, "cell_index": {c["id"]: c for c in cells}, "gu_band": gu_band, "seoul_monthly": seoul_monthly,
            "suppressed": dict(suppressed), "gus": sorted(gus, key=lambda g: g["gu"]),
            "monthly": dict(monthly), "excluded": excluded, "excluded_by_gu": excluded_by_gu, "stats": stats}


def _r(x, n=4):
    return None if x is None else round(x, n)


# ---------- 점수·추천 ----------

def budget_fit(price_mid, budget):
    if budget <= 0:
        return None
    u = price_mid / budget
    if 0.85 <= u <= 1.0:
        return 100.0
    if 0.6 <= u < 0.85:
        return 60 + 40 * (u - 0.6) / 0.25
    if u < 0.6:
        return 50.0
    return max(0.0, 100 - 200 * (u - 1.0))


def score_cell(cell, weights, budget=None):
    comps, tot_w, acc = [], 0.0, 0.0
    all_w = sum(weights.values())
    for k, w in weights.items():
        p = budget_fit(cell["price"]["med"], budget) if k == "budget_fit" else cell["pct"].get(k)
        if k == "budget_fit" and budget is None:
            p = None
        comps.append({"key": k, "label": LABELS[k], "weight": w, "pct": None if p is None else round(p, 1),
                      "value": cell["metrics"].get(k) if k != "budget_fit" else None})
        if p is not None:
            tot_w += w
            acc += w * p
    if tot_w < 0.70 * all_w:
        return None, comps
    return acc / tot_w, comps


def badges(cell):
    m = cell["metrics"]
    out = []
    y = m["yoy_raw"] if m.get("yoy_raw") is not None else m["yoy"]
    if y is not None:
        out.append({"key": "yoy", "text": f"1년 변화 {pct_txt(y)}", "dir": "up" if y > 0 else "down"})
    if m["vol"] is not None:
        out.append({"key": "vol", "text": f"거래량 1년 전보다 {pct_txt(math.exp(m['vol']) - 1)}", "dir": "up" if m["vol"] > 0 else "down"})
    if m["disc"] is not None:
        out.append({"key": "disc", "text": f"같은 구 평균보다 {pct_txt(-m['disc'])}", "dir": "down" if m["disc"] > 0 else "up"})
    if m["age"] is not None:
        out.append({"key": "age", "text": f"건물 나이 약 {int(m['age'])}년", "dir": "flat"})
    return out


def pct_txt(x):
    s = f"{abs(x) * 100:.1f}%"
    return ("+" if x > 0 else "−" if x < 0 else "±") + s


def reasons(comps):
    top = sorted([c for c in comps if c["pct"] is not None and c["key"] != "budget_fit"], key=lambda c: -c["pct"] * c["weight"])[:3]
    return [f"{c['label']}: 서울 상위 {max(1, round(100 - c['pct']))}%" for c in top]


def recommend(agg, weights, budget, gus=None, bands=None, sort="score", offset=0, limit=20, budget_by_band=None):
    """budget_by_band: {'L': 85㎡ 초과 농특세를 넣은 예산} 처럼 면적대별 예산(없으면 budget)."""
    bb = lambda c: (budget_by_band or {}).get(c["band"], budget)
    if budget is None or budget <= 0:
        return {"total": 0, "items": [], "empty_reason": "budget_zero", "peer_note": None}
    pool = [c for c in agg["cells"] if (not gus or c["gu"] in gus) and (not bands or c["band"] in bands)]
    band_summary = []
    for b in (bands or list(BANDS)):
        pb = [c for c in pool if c["band"] == b]
        wb = [c for c in pb if c["price"]["p25"] <= bb(c)]
        cheap = min(pb, key=lambda c: c["price"]["p25"]) if pb else None
        band_summary.append({"band": b, "label": BANDS[b], "cells": len(pb), "within": len(wb),
                             "cheapest": None if cheap is None else {"gu": cheap["gu"], "dong": cheap["dong"], "p25": cheap["price"]["p25"],
                                                                     "gap": max(0, cheap["price"]["p25"] - bb(cheap))}})
    if not pool:
        return {"total": 0, "items": [], "empty_reason": "no_cells_in_selection", "band_summary": band_summary}
    within = [c for c in pool if c["price"]["p25"] <= bb(c)]
    if not within:
        near = min(pool, key=lambda c: c["price"]["p25"] - bb(c))
        return {"total": 0, "items": [], "empty_reason": "all_over_budget", "band_summary": band_summary,
                "nearest": {"gap": near["price"]["p25"] - bb(near), "item": _item(near, weights, bb(near))}}
    items = [_item(c, weights, bb(c)) for c in within]
    penalty = {"A": 0, "B": 0, "C": 5}
    for x in items:
        v = x["score"]["value"]
        x["rank_score"] = None if v is None else round(v - penalty[x["grade"]], 1)
        x["rank_note"] = "자료가 적어(C) 순위를 정할 때 5점 낮춰 반영했어요" if (v is not None and penalty[x["grade"]]) else None
    if sort == "price":
        items.sort(key=lambda x: x["price"]["med"])
    elif sort == "n":
        items.sort(key=lambda x: -x["sample"]["n"])
    else:
        items.sort(key=lambda x: (-(x["score"]["value"] - penalty[x["grade"]] if x["score"]["value"] is not None else -999), -x["sample"]["n"]))
        # 다양성: 상위 10개 안에서 같은 구 최대 3개, 같은 동 1개
        top, rest, per_gu, dongs = [], [], Counter(), set()
        for x in items:
            k = (x["gu"], x["dong"])
            if len(top) < 10 and per_gu[x["gu"]] < 3 and k not in dongs:
                top.append(x)
                per_gu[x["gu"]] += 1
                dongs.add(k)
            else:
                if len(top) < 10:
                    x["rank_note"] = ((x["rank_note"] + " · ") if x["rank_note"] else "") + (
                        "같은 동의 다른 면적대가 이미 위에 있어 뒤로 보냈어요" if k in dongs else "같은 구 후보가 위에 3곳 있어 뒤로 보냈어요")
                rest.append(x)
        items = top + rest
    return {"total": len(items), "items": items[offset:offset + limit], "empty_reason": None, "band_summary": band_summary,
            "peer_note": f"서울 {len({c['gu'] for c in agg['cells']})}개 구, {len(agg['cells'])}개 동·면적대와 비교한 점수예요"}


def recommend_apts(agg, purpose, weights, budget_by_band, gus=None, bands=None, sort="score", limit=30, micro=False):
    """예산 안 단지 추천: 동네 점수 50% + 단지 점수 50%. 단지 가격(중앙값)이 예산을 넘으면 제외(하위 25%만 들어오면 '빠듯')."""
    aw = APT_WEIGHTS[purpose]
    out = []
    for c in agg["cells"]:
        if (gus and c["gu"] not in gus) or (bands and c["band"] not in bands):
            continue
        budget = budget_by_band.get(c["band"], 0)
        if budget <= 0:
            continue
        cell_score, _ = score_cell(c, weights, budget)
        for a in c["complexes"]:
            if a["price"]["p25"] > budget or (not micro and a["area_med"] < MICRO_AREA):
                continue
            tot, acc, parts = 0.0, 0.0, []
            for k, w in aw.items():
                pv = a["pct"].get(k)
                if pv is not None:
                    tot += w
                    acc += w * pv
                    parts.append({"key": k, "label": APT_LABELS[k], "pct": round(pv), "weight": w})
            apt_score = acc / tot if tot >= 0.5 else None
            if apt_score is None and cell_score is None:
                continue
            score = (0.5 * cell_score + 0.5 * apt_score) if (cell_score is not None and apt_score is not None) else (apt_score if apt_score is not None else cell_score)
            score -= {"A": 0, "B": 0, "C": 5}[c["grade"]] if a["n"] >= 3 else 8      # 거래 2건 단지·자료 C 동네는 순위에서 낮춤
            if a["bulk"]:
                score -= 5                                                             # 분양·일괄 계약으로 거래 수가 부풀었을 수 있음
            top = sorted(parts, key=lambda x: -x["pct"] * x["weight"])[:2]
            out.append({**{k: a[k] for k in ("apt_id", "cell_id", "apt_name", "built", "n", "n_prov", "n_days", "bulk", "area_med", "areas", "price", "ppm_med",
                                             "yoy", "disc", "last_ym", "gu", "dong", "band", "band_label")},
                        "fit": "full" if a["price"]["med"] <= budget else "partial",
                        "score": round(score, 1), "apt_score": None if apt_score is None else round(apt_score, 1),
                        "cell_score": None if cell_score is None else round(cell_score, 1), "cell_grade": c["grade"],
                        "parts": parts, "reasons": [f"{p['label']}: 같은 면적대 단지 중 상위 {max(1, 100 - p['pct'])}%" for p in top],
                        "few": a["n"] < 3})
    if sort == "price":
        out.sort(key=lambda x: x["price"]["med"])
    elif sort == "n":
        out.sort(key=lambda x: -x["n"])
    else:
        out.sort(key=lambda x: -x["score"])
    # 같은 동네 단지가 위를 다 차지하지 않도록: 위쪽 15곳에는 한 동네 3곳까지
    if sort == "score":
        top, rest, per = [], [], Counter()
        for x in out:
            k = (x["gu"], x["dong"])
            (top if len(top) < 15 and per[k] < 3 else rest).append(x)
            if len(top) and top[-1] is x:
                per[k] += 1
        out = top + rest
    return {"total": len(out), "items": out[:limit]}


WIDE_RANGE = 2.0      # 예상가 상단/하단이 이 배수를 넘으면 '단지마다 가격 차이가 큼'


def _item(c, weights, budget):
    s, comps = score_cell(c, weights, budget)
    wide = c["price"]["p25"] > 0 and c["price"]["p75"] / c["price"]["p25"] > WIDE_RANGE
    return {"id": c["id"], "gu": c["gu"], "dong": c["dong"], "band": c["band"], "band_label": c["band_label"],
            "rep_area": c["rep_area"], "price": c["price"], "ppm": c["ppm"], "wide_range": wide,
            "within_budget": "check" if wide else ("full" if c["price"]["med"] <= budget else "partial"),
            "grade": c["grade"], "score": {"value": None if s is None else round(s, 1), "components": comps},
            "reasons": reasons(comps), "badges": badges(c), "sample": c["sample"], "warnings": c["warnings"],
            "metrics": c["metrics"]}
