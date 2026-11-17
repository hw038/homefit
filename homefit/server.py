"""HomeFit 서울 — 마이데이터(가명) × 서울 실거래 내 집 찾기 MVP 웹 서버.

실행: python3 -I server.py [--port 8765] [--host 127.0.0.1]
- 런타임 외부 호출 없음. data/trades(비식별 실거래)와 mydata/pseudo(가명 CSV)만 읽는다.
- 응답에 본번·부번·건물명·단지해시(cid)·층·계약일·개별 거래와 고객 실명/주민번호/연락처/계좌번호를 넣지 않는다.
"""
import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import budget as B   # noqa: E402
import likes as L    # noqa: E402
import market as M   # noqa: E402
import queue         # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402


class LikeHub:
    """Valkey 구독 스레드 하나가 받은 알림을 접속 중인 브라우저(SSE)별 대기열로 나눠 준다."""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, msgs = L.subscribe()
                for m in msgs:
                    with self.lock:
                        for q in list(self.clients):
                            try:
                                q.put_nowait(m)
                            except queue.Full:
                                pass
            except (OSError, ConnectionError, L.RespError):
                time.sleep(3)                      # Valkey 가 꺼져 있으면 잠시 뒤 다시 붙는다

    def add(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.add(q)
        return q

    def remove(self, q):
        with self.lock:
            self.clients.discard(q)

GUS = ["강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구", "노원구", "도봉구", "동대문구", "동작구",
       "마포구", "서대문구", "서초구", "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구"]
DEMO = [
    {"customer_id": "C0343", "label": "사회초년생", "desc": "26세 · 대출 없음 · 자기자본 약 3,300만원"},
    {"customer_id": "C0924", "label": "중산층 직장인", "desc": "48세 · 연소득 약 7,600만원 · 대출 없음"},
    {"customer_id": "C0385", "label": "고자산", "desc": "62세 · 가용자산 서울 고객 중 최대"},
    {"customer_id": "C0032", "label": "1주택 갈아타기", "desc": "46세 · 주택담보대출 보유"},
    {"customer_id": "C0698", "label": "전세 세입자", "desc": "44세 · 전세자금대출 · DC/IRP 보유"},
    {"customer_id": "C0424", "label": "고부채", "desc": "56세 · 신용대출 3.5억 · DSR 초과"},
]
SECRET_PATH = ROOT / "data" / ".origin_secret"      # CloudFront 가 붙여 보내는 비밀 헤더 값(파일 권한 600)
ORIGIN_HEADER = "X-HF-Origin"


def load_secret():
    try:
        v = SECRET_PATH.read_text(encoding="utf-8").strip()
        return v if len(v) >= 32 else None
    except OSError:
        return None


class RateLimit:
    """IP 별 1분 창 요청 수 제한(공개 경로에만 적용)."""

    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def ok(self, key, limit):
        now = int(time.time() // 60)
        with self.lock:
            if len(self.hits) > 20000:
                self.hits = {k: v for k, v in self.hits.items() if v[0] == now}
            m, n = self.hits.get(key, (now, 0))
            if m != now:
                m, n = now, 0
            if n >= limit:
                return False
            self.hits[key] = (m, n + 1)
            return True


LIMITS = {"like": 60, "view": 120, "recommend": 120, "other": 600}
SSE_MAX = 300
FORBIDDEN_KEYS = {"cid", "MNO", "SNO", "BLDG_NM", "FLR", "CTRT_DAY", "name", "rrn", "mobile", "account_no", "card_no",
                  "account_id", "gender"}
DISCLAIMER = ("가명 데이터로 만든 모의 계산입니다. 실제 대출한도와 세금은 금융회사 심사와 정책에 따라 다릅니다. "
              "점수는 과거 실거래 지표를 요약한 참고 정보이며 가격 예측이나 투자 권유가 아닙니다.")


class App:
    def __init__(self, data_dir=M.DATA_DIR, pseudo_dir=B.PSEUDO_DIR, policy_path=B.POLICY_PATH):
        self.policy = B.load_policy(policy_path)
        self.store = B.CustomerStore(pseudo_dir, self.policy)
        rows, self.coverage = M.load_rows(data_dir)
        self.capped = sorted(f"{g}_{ym}" for g, v in self.coverage.items() for ym, n in v.items() if n >= 1000)
        self.partial = sorted(partial_months(self.coverage))
        cs = Path(data_dir).parent / "collection_status.json"
        self.collection = json.loads(cs.read_text(encoding="utf-8")) if cs.exists() else {}
        self.agg = M.build(rows, self.coverage, self.capped, self.partial, self.collection.get("tool_limit", []))
        self.apt_index = {a["apt_id"]: a for c in self.agg["cells"] for a in c["complexes"]}
        self.public_ids = {d["customer_id"] for d in DEMO} | {cid for cid, c in self.store.customers.items() if c["region"] == "서울"}
        self.secret = load_secret()
        self.rl = RateLimit()
        self.sse_n = 0
        self.likes = L.Likes()
        # 거래 급증: 최근 3개월 거래일이 직전 3개월보다 크게 는 단지(신축·자료 부족 구 제외)
        surge = [a for a in self.apt_index.values() if a.get("surge") and a["surge"]["recent_days"] >= 4
                 and a["surge"]["ratio"] >= 1.5 and not (a["built"] and a["built"] >= 2025)]
        self.deal_surge = sorted(surge, key=lambda a: (-a["surge"]["ratio"], -a["surge"]["recent_days"]))[:15]
        self.hub = LikeHub()
        self.raw_rows = len(rows)
        hp = Path(data_dir).parent / "policy_history.json"
        self.policy_history = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else {"events": []}
        vp = Path(data_dir).parent / "verification.json"
        self.verification = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else None

    # ---- handlers: 모두 (status, dict) 반환 ----
    def meta(self, q, public=False):
        cov = []
        for g in GUS:
            months = sorted(self.coverage.get(g, {}))
            have_recent = any(m in M.P1 for m in months)
            cov.append({"gu": g, "months": months,
                        "status": "ok" if have_recent and any(m in M.P0 for m in months) else ("no_recent" if not have_recent else "no_prior")})
        return 200, {"data_asof": "2026-11-17", "service": "서울 열린데이터광장 부동산 실거래가 정보",
                     "periods": {"prior": list(M.P0), "recent": list(M.P1), "provisional": list(M.PN)},
                     "coverage": cov, "capped_files": self.capped, "partial_files": self.partial, "unavailable_files": self.collection.get("tool_limit", []), "raw_rows": self.raw_rows,
                     "verification": {"method": self.verification.get("method")} if self.verification else None,  # 내부 검증 메모는 내보내지 않음
                     "stats": self.agg["stats"],
                     "policy": {k: self.policy[k] for k in ("asof", "basis", "region", "ltv", "housing_labels", "loan_cap_by_price",
                                                             "dsr_limit", "stress_rate_pp", "base_rate_pct", "term_years", "toheo_all_seoul")},
                     "weights": self.policy["weights"], "labels": M.LABELS,
                     "thresholds": {"min_n": M.MIN_CELL_N, "min_complex": M.MIN_CELL_CID, "low_sample": M.LOW_SAMPLE,
                                    "direct_low": M.DIRECT_LOW, "outlier_z": M.OUTLIER_Z, "shrink_k": M.SHRINK_K},
                     "gus": GUS, "demo": DEMO, "disclaimer": DISCLAIMER, "public": bool(public)}

    def customers(self, q, public=False):
        try:
            limit = int(q.get("limit", ["50"])[0])
        except ValueError:
            return 400, err("BAD_REQUEST", "limit 은 정수여야 합니다")
        r = self.store.list(q.get("q", [""])[0][:5], q.get("region", [None])[0], 1000)
        if public:
            r["items"] = [x for x in r["items"] if x["customer_id"] in self.public_ids]
            r["total"] = len(r["items"])
        r["items"] = r["items"][:max(1, min(limit, 1000))]
        return 200, r

    def summary(self, cid):
        p = self.store.profile(cid)
        if p is None:
            return 404, err("NOT_FOUND", "고객을 찾을 수 없습니다")
        return 200, {k: v for k, v in p.items() if k != "_raw"}

    def _budget(self, body):
        p = self.store.profile(str(body.get("customer_id", "")))
        if p is None:
            raise LookupError
        housing = body.get("housing") or None
        if housing is not None and housing not in self.policy["ltv"]:
            raise ValueError("housing 값이 올바르지 않습니다")
        rate = body.get("rate")
        if rate is not None:
            rate = float(rate)
            if not 2.0 <= rate <= 8.0:
                raise ValueError("금리는 2.0~8.0% 범위여야 합니다")
        purpose = body.get("purpose", "live")
        if purpose not in ("live", "invest"):
            raise ValueError("purpose 는 live|invest")
        kw = dict(housing=housing, rate=rate, include_costs=_bool(body, "include_costs", True),
                  use_pension=_bool(body, "use_pension", False), purpose=purpose,
                  extra=_num(body.get("extra", 0), 0, 500000), use_jeonse=_bool(body, "use_jeonse", False))
        b = B.budget_with_sensitivity(p, self.policy, **kw)
        # 85㎡ 초과는 농어촌특별세가 붙으므로 그 면적대 후보는 따로 계산한 예산과 비교한다
        kw.pop("rate")
        b_large = B.compute_budget(p, self.policy, rate=b["rate"], large=True, **kw)
        return p, b, b_large, purpose

    def budget(self, body):
        try:
            _, b, _, _ = self._budget(body)
        except LookupError:
            return 404, err("NOT_FOUND", "고객을 찾을 수 없습니다")
        except (ValueError, TypeError) as e:
            return 400, err("BAD_REQUEST", str(e))
        return 200, public_budget(b)

    def recommend(self, body):
        try:
            _, b, b_large, purpose = self._budget(body)
            gus = _str_list(body, "gu", GUS)
            bands = _str_list(body, "bands", list(M.BANDS))
            sort = body.get("sort", "score")
            if sort not in ("score", "price", "n"):
                raise ValueError("sort 는 score|price|n")
            offset = _int(body.get("offset", 0), 0, 100000)
            limit = _int(body.get("limit", 20), 1, 50)
        except LookupError:
            return 404, err("NOT_FOUND", "고객을 찾을 수 없습니다")
        except (ValueError, TypeError) as e:
            return 400, err("BAD_REQUEST", str(e))
        by_band = {"S": b["budget_max"], "M": b["budget_max"], "L": b_large["budget_max"]}
        r = M.recommend(self.agg, self.policy["weights"][purpose], b["budget_max"], gus, bands, sort, offset, limit,
                        budget_by_band=by_band)
        # 구별 예산 내 후보 수 (히트맵용)
        counts = {}
        for c in self.agg["cells"]:
            if (not bands or c["band"] in bands) and by_band[c["band"]] > 0 and c["price"]["p25"] <= by_band[c["band"]]:
                counts[c["gu"]] = counts.get(c["gu"], 0) + 1
        micro = body.get("micro", False)
        if not isinstance(micro, bool):
            return 400, err("BAD_REQUEST", "micro 는 true/false 여야 합니다")
        apts = M.recommend_apts(self.agg, purpose, self.policy["weights"][purpose], by_band, gus, bands, sort, limit=60, micro=micro)
        extra = {"plan": plan(self.agg, {"S": b, "M": b, "L": b_large}, gus, bands)} \
            if r.get("empty_reason") in ("all_over_budget", "budget_zero") or r["total"] < 3 else {}
        return 200, {"budget": public_budget(b), **r, **extra, "gu_counts": counts, "purpose": purpose, "apartments": apts}

    def market_gu(self, q):
        return 200, {"items": self.agg["gus"]}

    def market_gu_detail(self, gu):
        if gu not in GUS:
            return 404, err("NOT_FOUND", "자치구 이름이 올바르지 않습니다")
        cells = [{k: c[k] for k in ("id", "gu", "dong", "band", "band_label", "rep_area", "ppm", "price", "sample", "grade", "metrics", "warnings")}
                 for c in self.agg["cells"] if c["gu"] == gu]
        cells.sort(key=lambda c: (c["dong"], c["band"]))
        return 200, {"gu": gu, "cells": cells, "monthly": self.agg["monthly"].get(gu, []),
                     "excluded": self.agg["excluded_by_gu"].get(gu, {})}

    def cell(self, q):
        cid = (q.get("id") or [""])[0]
        c = self.agg["cell_index"].get(cid)
        if c is None:
            return 404, err("NOT_FOUND", "해당 동·면적대 시세가 없습니다")
        g = self.agg["gu_band"].get(f"{c['gu']}|{c['band']}", {})
        return 200, {k: c[k] for k in ("id", "gu", "dong", "band", "band_label", "rep_area", "ppm", "price", "sample", "grade", "warnings")} | {
            "metrics": {k: c["metrics"][k] for k in ("yoy", "yoy_raw", "yoy_method", "mom", "disc", "vol")},
            "trend": c["trend"],
            "complexes": sorted(({k: v for k, v in a.items() if k != "pct"} for a in c["complexes"]), key=lambda a: -a["n"]),
            "gu_band": {"label": f"{c['gu']} 전체 · {c['band_label']}", "monthly": g.get("monthly", []),
                        "periods": g.get("periods", {}), "yoy": g.get("yoy"), "yoy_method": g.get("yoy_method")}}

    def policy_hist(self, q):
        sm = {m["ym"]: m for m in self.agg["seoul_monthly"]}
        def comp(a, b):
            na, nb = sum(sm[m]["n"] or 0 for m in a), sum(sm[m]["n"] or 0 for m in b)
            return {"before": na, "after": nb, "chg": round(nb / na - 1, 4) if na else None}
        p = self.policy
        current = [
            {"label": "규제지역", "value": p["region"]},
            {"label": "LTV", "value": " · ".join(f"{p['housing_labels'][k]} {round(v * 100)}%" for k, v in p["ltv"].items())},
            {"label": "주담대 상한", "value": " · ".join(f"{t['label']} {t['cap'] // 10000}억" for t in p["loan_cap_by_price"])},
            {"label": "DSR · 스트레스 금리", "value": f"{round(p['dsr_limit'] * 100)}% · +{p['stress_rate_pp']}%p"},
            {"label": "만기", "value": f"최장 {p['term_years']}년"},
        ]
        return 200, {**self.policy_history, "current_rules": current, "policy_asof": p["asof"],
                     "observed": {"seoul_monthly": self.agg["seoul_monthly"],
                                  "volume_aug_sep": comp(("202508", "202509"), ("202608", "202609")),
                                  "note": "취소·중복·이상 거래를 뺀 서울 아파트 거래 수예요. 2025년 10월에는 10·15 대책 전후 거래가 섞여 있고, "
                                          "강동구 2025년 9월과 동대문구 2025년 10월은 거래가 많아 일부가 빠졌을 수 있어요. "
                                          "거래량에는 정책뿐 아니라 금리와 계절도 영향을 줘요."}}

    def likes_get(self, q):
        ids = [x for x in (q.get("ids") or [""])[0].split(",") if x][:200]
        voter = (q.get("voter") or [""])[0]
        voter = voter if L.VOTER_RE.match(voter) else None
        try:
            return 200, {"available": True, "counts": self.likes.counts(ids, voter)}
        except (OSError, ConnectionError, L.RespError):
            return 200, {"available": False, "counts": {}}

    def likes_top(self, q):
        try:
            top = self.likes.top(10)
        except (OSError, ConnectionError, L.RespError):
            return 200, {"available": False, "items": []}
        items = []
        for t in top:
            a = self.apt_index.get(t["apt_id"])
            items.append({"apt_id": t["apt_id"], "count": t["count"],
                          "label": (f"{a['gu']} {a['dong']} {a['apt_name'] or ''} · {a['band_label']}".strip() if a else t["label"]) or "단지 정보 없음",
                          "cell_id": a["cell_id"] if a else None})
        return 200, {"available": True, "items": items}

    def _apt_card(self, a):
        return {"apt_id": a["apt_id"], "cell_id": a["cell_id"], "apt_name": a["apt_name"], "gu": a["gu"], "dong": a["dong"],
                "band_label": a["band_label"], "built": a["built"], "price_med": a["price"]["med"], "n": a["n"], "surge": a.get("surge")}

    def trending(self, q):
        deals = [self._apt_card(a) for a in self.deal_surge[:10]]
        try:
            hot = []
            for t in self.likes.trending(10):
                a = self.apt_index.get(t["apt_id"])
                if a:
                    hot.append({**self._apt_card(a), **{k: t[k] for k in ("score", "likes_24h", "views_24h", "likes")}})
            return 200, {"available": True, "likes": hot, "deals": deals}
        except (OSError, ConnectionError, L.RespError):
            return 200, {"available": False, "likes": [], "deals": deals}

    def apt_view(self, body):
        apt, voter = body.get("apt_id"), body.get("voter")
        a = self.apt_index.get(apt) if isinstance(apt, str) else None
        if a is None or not isinstance(voter, str):
            return 400, err("BAD_REQUEST", "apt_id, voter 가 필요해요")
        try:
            counted = self.likes.view(apt, voter, f"{a['gu']} {a['dong']} {a['apt_name'] or ''} · {a['band_label']}")
        except ValueError as e:
            return 400, err("BAD_REQUEST", str(e))
        except (OSError, ConnectionError, L.RespError):
            return 200, {"counted": False}
        if counted:
            try:
                self.likes._call(lambda c: c.cmd("PUBLISH", L.CHANNEL, json.dumps({"apt_id": apt, "view": 1})))
            except (OSError, ConnectionError, L.RespError):
                pass
        return 200, {"counted": counted}

    def likes_post(self, body):
        apt, voter, on = body.get("apt_id"), body.get("voter"), body.get("on")
        if not isinstance(apt, str) or not isinstance(voter, str) or not isinstance(on, bool):
            return 400, err("BAD_REQUEST", "apt_id, voter, on 이 필요해요")
        a = self.apt_index.get(apt)
        if a is None:
            return 404, err("NOT_FOUND", "단지를 찾을 수 없어요")
        try:
            label = f"{a['gu']} {a['dong']} {a['apt_name'] or ''} · {a['band_label']}"
            return 200, self.likes.toggle(apt, voter, on, label)
        except ValueError as e:
            return 400, err("BAD_REQUEST", str(e))
        except (OSError, ConnectionError, L.RespError):
            return 503, err("UNAVAILABLE", "좋아요 저장소에 연결하지 못했어요. 잠시 뒤 다시 시도해 주세요")

    def exclusions(self, q):
        total = self.agg["stats"]["apt_rows"]
        by_reason = [{"reason": k, "n": v, "share": round(v / total, 4) if total else 0} for k, v in
                     sorted(self.agg["excluded"].items(), key=lambda x: -x[1])]
        by_gu = [{"gu": g, "n": sum(v.values())} for g, v in self.agg["excluded_by_gu"].items()]
        return 200, {"by_reason": by_reason, "by_gu": sorted(by_gu, key=lambda x: -x["n"]), "apt_rows": total,
                     "suppressed_cells": self.agg["suppressed"],
                     "notes": ["개인정보 보호를 위해 동·호수 정보는 쓰지 않아요. 그래서 같은 집이 짧은 기간에 여러 번 거래됐는지는 따로 걸러내지 않았어요.",
                               "공공데이터에 같은 거래가 두 번 들어간 경우가 있어 하나만 남겼어요."]}


def partial_months(coverage, ratio=0.4):
    """같은 구의 다른 달(잠정 제외) 거래 수 중앙값의 40%에도 못 미치는 달 → 조회 한도 등으로 일부가 빠졌을 가능성."""
    import statistics
    out = set()
    for gu, months in coverage.items():
        full = [n for ym, n in months.items() if ym not in M.PN]
        if len(full) < 4:
            continue
        med = statistics.median(full)
        out |= {f"{gu}_{ym}" for ym, n in months.items() if ym not in M.PN and n < ratio * med}
    return out


def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ValueError("숫자가 필요합니다")
    if not lo <= v <= hi:
        raise ValueError(f"값은 {lo}~{hi} 범위여야 합니다")
    return float(v)


def _int(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("정수가 필요합니다")
    if not lo <= v <= hi:
        raise ValueError(f"값은 {lo}~{hi} 범위여야 합니다")
    return v


def _bool(body, key, default):
    v = body.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"{key} 는 true/false 여야 합니다")
    return v


def _str_list(body, key, allowed):
    v = body.get(key) or []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{key} 는 문자열 목록이어야 합니다")
    bad = [x for x in v if x not in allowed]
    if bad:
        raise ValueError(f"{key} 에 알 수 없는 값: {', '.join(bad[:3])}")
    return v


def plan(agg, by_band, gus, bands, k=5):
    """예산 밖이면: 가장 싼 후보 k곳과 필요한 추가 자기자본, 월 잉여로 모으는 기간."""
    pool = [c for c in agg["cells"] if (not gus or c["gu"] in gus) and (not bands or c["band"] in bands)]
    pool.sort(key=lambda c: c["price"]["p25"])
    surplus = by_band["S"]["_surplus_m"]
    out = []
    for c in pool[:k]:
        b = by_band[c["band"]]
        need_fn = b["_equity_needed"]
        target = c["price"]["p25"]
        gap = max(0.0, need_fn(target) - b["equity_used"])
        out.append({"gu": c["gu"], "dong": c["dong"], "band": c["band"], "band_label": c["band_label"], "price_p25": target,
                    "price_med": c["price"]["med"], "grade": c["grade"], "equity_needed": round(need_fn(target)),
                    "equity_gap": round(gap), "months_to_save": (round(gap / surplus) if surplus > 0 else None) if gap > 0 else 0})
    return {"items": out, "surplus_monthly": round(surplus)}


def public_budget(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def err(code, msg):
    return {"error": {"code": code, "message": msg}}


def scrub(obj):
    """응답에 금지 키가 섞이지 않았는지 재귀 검사 (방어적)."""
    if isinstance(obj, dict):
        bad = FORBIDDEN_KEYS & set(obj)
        if bad:
            raise RuntimeError(f"forbidden keys in response: {sorted(bad)}")
        for v in obj.values():
            scrub(v)
    elif isinstance(obj, list):
        for v in obj:
            scrub(v)
    return obj


def make_handler(app):
    index = (ROOT / "static" / "index.html")

    class H(BaseHTTPRequestHandler):
        server_version = "HomeFit/0.1"
        timeout = 15

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(scrub(body), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)

        def _sse(self):
            """좋아요 변경을 실시간으로 밀어 주는 Server-Sent Events. 15초마다 연결 확인 신호를 보낸다."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = app.hub.add()
            app.sse_n += 1
            try:
                self.wfile.write(b"retry: 3000\nevent: hello\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        m = q.get(timeout=15)
                        self.wfile.write(f"event: like\ndata: {m}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.sse_n -= 1
                app.hub.remove(q)
                self.close_connection = True

        def do_GET(self):
            try:
                self._get()
            except Exception:                     # noqa: BLE001 — 내부 오류도 JSON 으로 응답
                self._send(500, err("INTERNAL", "서버 내부 오류"))

        def do_POST(self):
            try:
                self._post()
            except Exception:                     # noqa: BLE001
                self._send(500, err("INTERNAL", "서버 내부 오류"))

        def _public(self):
            """CloudFront 를 거친 공개 요청이면 True. 로컬(127.0.0.1, VS Code 프록시)은 False. 그 밖의 직접 접속은 None(거절)."""
            if self.client_address[0] in ("127.0.0.1", "::1"):
                return False
            if app.secret and self.headers.get(ORIGIN_HEADER) == app.secret:
                return True
            return None

        def _ip(self):
            # CloudFront 가 직접 채우는 헤더만 믿는다(원본 요청 정책 AllViewerAndCloudFrontHeaders 가 항상 전달).
            cv = self.headers.get("CloudFront-Viewer-Address")           # "IP:포트" (IPv6 도 마지막 : 뒤가 포트)
            if cv:
                return cv.rsplit(":", 1)[0].strip("[] ")
            # 없으면 X-Forwarded-For 의 맨 오른쪽(CloudFront 가 덧붙인 값)만 쓴다. 맨 왼쪽은 접속자가 위조할 수 있다.
            xf = self.headers.get("X-Forwarded-For")
            return xf.split(",")[-1].strip() if xf else self.client_address[0]

        def _guard(self, kind):
            pub = self._public()
            if pub is None:
                self._send(403, err("FORBIDDEN", "허용되지 않은 접속 경로예요"))
                return None
            if pub and not app.rl.ok((self._ip(), kind), LIMITS[kind]):
                self._send(429, err("TOO_MANY", "요청이 너무 많아요. 잠시 뒤 다시 시도해 주세요"))
                return None
            return pub

        def _get(self):
            u = urlparse(self.path)
            pub = self._guard("other")
            if pub is None:
                return
            path, q = unquote(u.path), parse_qs(u.query)
            if ".." in path:
                return self._send(400, err("BAD_REQUEST", "잘못된 경로"))
            if path in ("/", "/index.html"):
                return self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/likes/stream":
                if pub and app.sse_n >= SSE_MAX:
                    return self._send(503, err("BUSY", "실시간 연결이 많아요. 잠시 뒤 다시 시도해 주세요"))
                return self._sse()
            if path == "/api/customers":
                return self._send(*app.customers(q, public=pub))
            if path == "/api/meta":
                return self._send(*app.meta(q, public=pub))
            routes = {"/api/trending": app.trending, "/api/likes": app.likes_get, "/api/likes/top": app.likes_top, "/api/meta": app.meta, "/api/customers": app.customers, "/api/market/gu": app.market_gu, "/api/cell": app.cell, "/api/policy/history": app.policy_hist,
                      "/api/exclusions": app.exclusions}
            if path in routes:
                return self._send(*routes[path](q))
            m = re.fullmatch(r"/api/customers/(C\d{4})/summary", path)
            if m:
                if pub and m.group(1) not in app.public_ids:
                    return self._send(404, err("NOT_FOUND", "공개 화면에서는 데모 고객과 서울 거주 고객만 볼 수 있어요"))
                return self._send(*app.summary(m.group(1)))
            m = re.fullmatch(r"/api/market/gu/([^/]+)", path)
            if m:
                return self._send(*app.market_gu_detail(m.group(1)))
            return self._send(404, err("NOT_FOUND", "없는 경로"))

        def _post(self):
            path = urlparse(self.path).path
            kind = {"/api/likes": "like", "/api/apt/view": "view", "/api/recommend": "recommend", "/api/budget": "recommend"}.get(path, "other")
            pub = self._guard(kind)
            if pub is None:
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, err("BAD_REQUEST", "Content-Length 가 올바르지 않습니다"))
            if n < 0:
                return self._send(400, err("BAD_REQUEST", "Content-Length 가 올바르지 않습니다"))
            if n > 100_000:
                return self._send(413, err("BAD_REQUEST", "요청이 너무 큽니다"))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, err("BAD_REQUEST", "JSON 본문이 필요합니다"))
            if pub and isinstance(body.get("customer_id"), str) and path in ("/api/budget", "/api/recommend") \
                    and body["customer_id"] not in app.public_ids:
                return self._send(404, err("NOT_FOUND", "공개 화면에서는 데모 고객과 서울 거주 고객만 볼 수 있어요"))
            if path == "/api/budget":
                return self._send(*app.budget(body))
            if path == "/api/recommend":
                return self._send(*app.recommend(body))
            if path == "/api/likes":
                return self._send(*app.likes_post(body))
            if path == "/api/apt/view":
                return self._send(*app.apt_view(body))
            return self._send(404, err("NOT_FOUND", "없는 경로"))

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    app = App()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    srv.daemon_threads = True
    print(f"좋아요 저장소(Valkey): {'연결됨' if app.likes.available() else '연결 안 됨 — 좋아요 기능은 꺼진 채로 동작'}", flush=True)
    print(f"HomeFit 서울: http://{a.host}:{a.port}/  (셀 {len(app.agg['cells'])}개, 원천 {app.raw_rows}행)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
