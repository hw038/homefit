"""HomeFit 단위/통합 테스트 (네트워크 없이 핸들러를 메모리에서 구동)."""
import io, json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import budget as B, market as M, server as S

APP = S.App()

def call(method, path, body=None):
    H = S.make_handler(APP)
    raw = json.dumps(body or {}, ensure_ascii=False).encode() if method == "POST" else b""
    req = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(raw)}\r\nContent-Type: application/json\r\n\r\n".encode() + raw
    class Sock:
        def makefile(self, mode, *a, **k): return io.BytesIO(req) if "r" in mode else out
        def sendall(self, b): out.write(b)
    out = io.BytesIO()
    class Srv: pass
    h = H.__new__(H); h.rfile = io.BytesIO(req); h.wfile = out; h.client_address = ("127.0.0.1", 0); h.server = Srv()
    h.handle_one_request()
    head, _, payload = out.getvalue().partition(b"\r\n\r\n")
    status = int(head.split()[1])
    ctype = [l for l in head.decode().split("\r\n") if l.lower().startswith("content-type")][0]
    return status, (json.loads(payload) if "json" in ctype else payload.decode())

def walk_keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k; yield from walk_keys(v)
    elif isinstance(o, list):
        for v in o: yield from walk_keys(v)

POL = B.load_policy()

class Finance(unittest.TestCase):
    def test_pmt_pv_inverse(self):
        self.assertAlmostEqual(B.pv(B.pmt(30000, 7.5, 360), 7.5, 360), 30000, places=6)
    def test_loan_cap_tiers(self):
        self.assertEqual(B.loan_cap(150000, POL)[0], 60000)
        self.assertEqual(B.loan_cap(150100, POL)[0], 40000)
        self.assertEqual(B.loan_cap(250000, POL)[0], 40000)
        self.assertEqual(B.loan_cap(250100, POL)[0], 20000)
    def test_acq_tax(self):
        self.assertAlmostEqual(B.acq_tax(60000, "none", False, POL)[1], 0.01)
        self.assertAlmostEqual(B.acq_tax(75000, "none", False, POL)[1], 0.02)
        self.assertAlmostEqual(B.acq_tax(100000, "none", False, POL)[1], 0.03)
        self.assertAlmostEqual(B.acq_tax(100000, "multi", False, POL)[1], 0.08)
    def test_brokerage(self):
        self.assertAlmostEqual(B.brokerage(80000, POL), 80000 * 0.004 * 1.1)
        self.assertAlmostEqual(B.brokerage(10000, POL), 50 * 1.1)
    def _p(self, income, equity, service=0.0):
        return {"_raw": {"income": income, "equity": equity, "service": service, "pension_extra": 0, "surplus_m": 0}, "inferred_housing": "none"}
    def test_budget_feasible_and_maximal(self):
        p = self._p(7000, 30000)
        b = B.compute_budget(p, POL, housing="none")
        P = b["budget_max"]
        def ok(x):
            dsr, _ = B.dsr_loan_limit(7000, 0, POL["base_rate_pct"], POL["stress_rate_pp"], POL["dsr_limit"], 30)
            loan = min(POL["ltv"]["none"] * x, B.loan_cap(x, POL)[0], dsr)
            return x + B.costs(x, "none", False, POL)["total"] <= 30000 + loan + 1e-9
        self.assertTrue(ok(P)); self.assertFalse(ok(P + 100))
    def test_multi_no_loan(self):
        b = B.compute_budget(self._p(9000, 20000), POL, housing="multi")
        self.assertEqual(b["loan"]["amount"], 0); self.assertEqual(b["loan"]["binding"], "none_loan")
        self.assertLess(b["budget_max"], 20000)
    def test_dsr_exceeded_no_loan(self):
        b = B.compute_budget(self._p(3000, 10000, service=5000), POL, housing="none")
        self.assertEqual(b["loan"]["amount"], 0)
    def test_first_time_ltv_higher(self):
        a = B.compute_budget(self._p(20000, 30000), POL, housing="none")["budget_max"]
        f = B.compute_budget(self._p(20000, 30000), POL, housing="first")["budget_max"]
        self.assertGreaterEqual(f, a)
    def test_rate_monotone(self):
        p = self._p(6000, 20000)
        lo = B.compute_budget(p, POL, housing="none", rate=3.5)["budget_max"]
        hi = B.compute_budget(p, POL, housing="none", rate=6.5)["budget_max"]
        self.assertGreaterEqual(lo, hi)

class Profiles(unittest.TestCase):
    def test_demo_profiles(self):
        for d in S.DEMO:
            p = APP.store.profile(d["customer_id"])
            self.assertIsNotNone(p)
            self.assertGreater(p["income"]["annual"], 1000)
    def test_highdebt(self):
        p = APP.store.profile("C0424")
        self.assertEqual(p["equity"], 0)
        self.assertGreater(p["dsr_current"], 0.4)
    def test_bad_id(self):
        self.assertIsNone(APP.store.profile("../etc"))

class Market(unittest.TestCase):
    def test_cells_privacy_thresholds(self):
        for c in APP.agg["cells"]:
            self.assertGreaterEqual(c["sample"]["n"], M.MIN_CELL_N)
            self.assertGreaterEqual(c["sample"]["complexes"], M.MIN_CELL_CID)
            self.assertLessEqual(c["price"]["p25"], c["price"]["med"]); self.assertLessEqual(c["price"]["med"], c["price"]["p75"])
    def test_band(self):
        self.assertEqual(M.band_of(60.0), "S"); self.assertEqual(M.band_of(84.99), "M"); self.assertEqual(M.band_of(85.01), "L")
    def test_dedup_and_cancel(self):
        base = {"CGG_NM": "가구", "STDG_CD": "1", "STDG_NM": "가동", "CTRT_DAY": "20260801", "THING_AMT": 50000, "ARCH_AREA": 59.9,
                "FLR": 5, "RTRCN_DAY": "", "ARCH_YR": "2000", "BLDG_USG": "아파트", "DCLR_SE": "중개거래", "cid": "x", "ym": "202608"}
        rows = [dict(base), dict(base), dict(base, RTRCN_DAY="20260810", THING_AMT=51000)]
        valid, cancels, ex, _, _ = M.clean(rows)
        self.assertEqual(len(valid), 1); self.assertEqual(len(cancels), 1)
        self.assertEqual(ex["같은 거래가 두 번 들어간 기록"], 1)
    def test_direct_low(self):
        rows = [{"CGG_NM": "가구", "STDG_CD": "1", "STDG_NM": "가동", "CTRT_DAY": f"202608{10+i:02d}", "THING_AMT": 60000 + i, "ARCH_AREA": 59.9,
                 "FLR": i, "RTRCN_DAY": "", "ARCH_YR": "2000", "BLDG_USG": "아파트", "DCLR_SE": "중개거래", "cid": "x", "ym": "202608"} for i in range(6)]
        rows.append(dict(rows[0], DCLR_SE="직거래", THING_AMT=30000, FLR=20))
        valid, _, ex, _, _ = M.clean(rows)
        self.assertEqual(ex.get("시세보다 크게 싼 직거래(시세의 70% 미만)"), 1)

class Api(unittest.TestCase):
    def test_meta_and_index(self):
        s, m = call("GET", "/api/meta"); self.assertEqual(s, 200); self.assertEqual(len(m["coverage"]), 25)
        s, html = call("GET", "/"); self.assertEqual(s, 200); self.assertIn("HomeFit", html)
    def test_recommend_all_demo_no_pii(self):
        for d in S.DEMO:
            for purpose in ("live", "invest"):
                s, r = call("POST", "/api/recommend", {"customer_id": d["customer_id"], "purpose": purpose, "bands": ["S", "M", "L"]})
                self.assertEqual(s, 200, r)
                self.assertFalse(S.FORBIDDEN_KEYS & set(walk_keys(r)))
                for it in r["items"]:
                    self.assertLessEqual(it["price"]["p25"], r["budget"]["budget_max"])
    def test_summary_no_pii(self):
        s, p = call("GET", "/api/customers/C0343/summary"); self.assertEqual(s, 200)
        self.assertNotIn("_raw", p); self.assertFalse(S.FORBIDDEN_KEYS & set(walk_keys(p)))
        txt = json.dumps(p, ensure_ascii=False); self.assertNotIn("CI_", txt); self.assertNotIn("고객0343", txt)
    def test_market_endpoints_no_pii(self):
        for path in ["/api/market/gu", "/api/market/gu/마포구", "/api/exclusions", "/api/customers?region=서울"]:
            s, r = call("GET", path.replace("마포구", "%EB%A7%88%ED%8F%AC%EA%B5%AC")); self.assertEqual(s, 200, path)
            self.assertFalse(S.FORBIDDEN_KEYS & set(walk_keys(r)), path)
    def test_bad_inputs(self):
        self.assertEqual(call("GET", "/api/customers/C9999/summary")[0], 404)
        self.assertEqual(call("GET", "/api/customers/../x/summary")[0], 400)
        self.assertEqual(call("POST", "/api/recommend", {"customer_id": "C0343", "rate": 99})[0], 400)
        self.assertEqual(call("POST", "/api/recommend", {"customer_id": "C0343", "housing": "x"})[0], 400)
        self.assertEqual(call("GET", "/api/market/gu/없는구")[0], 404)
        self.assertEqual(call("GET", "/static/../server.py")[0], 400)



def raw_call(req):
    H = S.make_handler(APP)
    out = io.BytesIO()
    class Srv: pass
    h = H.__new__(H); h.rfile = io.BytesIO(req); h.wfile = out; h.client_address = ("127.0.0.1", 0); h.server = Srv()
    h.handle_one_request()
    return int(out.getvalue().split()[1])


class Review(unittest.TestCase):
    """검토 워크플로에서 확인된 결함의 회귀 테스트."""
    def test_items_exist_with_extra_funds(self):
        s, r = call("POST", "/api/recommend", {"customer_id": "C0343", "housing": "first", "extra": 30000, "bands": ["S", "M", "L"]})
        self.assertEqual(s, 200); self.assertGreater(len(r["items"]), 0)
        for it in r["items"]:
            self.assertLessEqual(it["price"]["p25"], r["budget"]["budget_max"] + 1)
        self.assertFalse(S.FORBIDDEN_KEYS & set(walk_keys(r)))
    def test_gu_filter_applied(self):
        s, r = call("POST", "/api/recommend", {"customer_id": "C0385", "extra": 50000, "gu": ["노원구"]})
        self.assertEqual(s, 200); self.assertTrue(r["items"]); self.assertTrue(all(i["gu"] == "노원구" for i in r["items"]))
    def test_pagination_disjoint(self):
        body = {"customer_id": "C0385", "extra": 80000, "bands": ["S", "M", "L"], "limit": 10}
        a = call("POST", "/api/recommend", dict(body, offset=0))[1]["items"]
        b = call("POST", "/api/recommend", dict(body, offset=10))[1]["items"]
        self.assertFalse({x["id"] for x in a} & {x["id"] for x in b})
    def test_bad_types_400(self):
        for bad in [{"offset": "x"}, {"limit": 1.5}, {"gu": "강남구"}, {"bands": {"a": 1}}, {"gu": [["x"]]}, {"gu": ["없는구"]},
                    {"extra": -1}, {"extra": "abc"}, {"include_costs": "false"}, {"use_jeonse": 1}, {"sort": "x"}]:
            st = call("POST", "/api/recommend", dict({"customer_id": "C0343"}, **bad))[0]
            self.assertEqual(st, 400, bad)
    def test_content_length(self):
        for cl in ("-1", "abc"):
            self.assertEqual(raw_call(f"POST /api/recommend HTTP/1.1\r\nHost: x\r\nContent-Length: {cl}\r\n\r\n".encode()), 400)
        self.assertEqual(raw_call(b"POST /api/recommend HTTP/1.1\r\nHost: x\r\nContent-Length: 200000\r\n\r\n"), 413)
    def test_jeonse_interest_removed(self):
        p = APP.store.profile("C0698")
        off = B.compute_budget(p, POL, use_jeonse=False)
        on = B.compute_budget(p, POL, use_jeonse=True)
        self.assertGreater(on["loan"]["limits"]["dsr"]["room_annual"], off["loan"]["limits"]["dsr"]["room_annual"])
        self.assertGreaterEqual(on["budget_max"], off["budget_max"])
    def test_large_band_budget_lower(self):
        p = {"_raw": {"income": 12000, "equity": 60000, "service": 0, "pension_extra": 0, "surplus_m": 0}, "inferred_housing": "none"}
        self.assertLess(B.compute_budget(p, POL, large=True)["budget_max"], B.compute_budget(p, POL)["budget_max"])
    def test_multi3_tax(self):
        self.assertAlmostEqual(B.acq_tax(100000, "multi3", False, POL)[1], 0.12)
    def test_price_tier_label_only_at_cap_boundary(self):
        p = {"_raw": {"income": 7500, "equity": 58000, "service": 0, "pension_extra": 0, "surplus_m": 0}, "inferred_housing": "none"}
        self.assertNotEqual(B.compute_budget(p, POL)["loan"]["binding"], "price_tier")
    def test_pension_warning(self):
        p = APP.store.profile("C0032")
        w = B.compute_budget(p, POL, housing="one_cond", use_pension=True)["warnings"]
        self.assertFalse(any("미리 꺼내 쓴다고 계산" in x for x in w))
    def test_no_gender_and_seoul_list(self):
        s, r = call("GET", "/api/customers?region=%EC%84%9C%EC%9A%B8&limit=1000")
        self.assertEqual(s, 200); self.assertEqual(len(r["items"]), r["total"]); self.assertNotIn("gender", r["items"][0])
    def test_uncollected_months_not_zero(self):
        for g in APP.agg["gus"]:
            if g["status"] != "ok":
                self.assertIsNone(g["vol_chg"], g["gu"]); self.assertIsNone(g["yoy"], g["gu"])
            for m in APP.agg["monthly"].get(g["gu"], []):
                if m["ppm_med"] is not None:
                    self.assertGreaterEqual(m["n"], M.MIN_CELL_N)



class TrendPolicy(unittest.TestCase):
    def test_cell_trend_privacy(self):
        import urllib.parse
        for c in APP.agg["cells"][:80]:
            st, r = call("GET", "/api/cell?id=" + urllib.parse.quote(c["id"]))
            self.assertEqual(st, 200)
            self.assertFalse(S.FORBIDDEN_KEYS & set(walk_keys(r)))
            for m in r["trend"]["monthly"] + r["gu_band"]["monthly"]:
                if m["ppm_med"] is not None:
                    self.assertGreaterEqual(m["n"], M.MIN_CELL_N)
        self.assertEqual(call("GET", "/api/cell?id=nope")[0], 404)
    def test_policy_history(self):
        st, r = call("GET", "/api/policy/history")
        self.assertEqual(st, 200)
        self.assertTrue(r["events"]); self.assertIn("caveat", r)
        self.assertEqual(len(r["observed"]["seoul_monthly"]), len(M.TREND_MONTHS))
        self.assertIsNotNone(r["observed"]["volume_aug_sep"]["chg"])


def pub_call(method, path, body=None, secret="ok", ip="10.0.0.5"):
    """CloudFront 를 거친 공개 요청 흉내: 다른 IP + (선택) 비밀 헤더."""
    H = S.make_handler(APP)
    raw = json.dumps(body or {}, ensure_ascii=False).encode() if method == "POST" else b""
    hdr = f"X-HF-Origin: {APP.secret if secret == 'ok' else secret}\r\n" if secret else ""
    req = (f"{method} {path} HTTP/1.1\r\nHost: x\r\n{hdr}CloudFront-Viewer-Address: 203.0.113.9:4567\r\n"
           f"Content-Length: {len(raw)}\r\nContent-Type: application/json\r\n\r\n").encode() + raw
    out = io.BytesIO()
    class Srv: pass
    h = H.__new__(H); h.rfile = io.BytesIO(req); h.wfile = out; h.client_address = (ip, 0); h.server = Srv()
    h.handle_one_request()
    head, _, payload = out.getvalue().partition(b"\r\n\r\n")
    return int(head.split()[1]), (json.loads(payload) if payload[:1] in b"{[" else payload)


class PublicMode(unittest.TestCase):
    def setUp(self):
        self._old = APP.secret
        APP.secret = "s" * 40
    def tearDown(self):
        APP.secret = self._old
    def test_direct_access_blocked(self):
        self.assertEqual(pub_call("GET", "/api/meta", secret=None)[0], 403)
        self.assertEqual(pub_call("GET", "/api/meta", secret="wrong" * 8)[0], 403)
    def test_public_flag_and_scope(self):
        s, m = pub_call("GET", "/api/meta"); self.assertEqual(s, 200); self.assertTrue(m["public"])
        s, r = pub_call("GET", "/api/customers?limit=1000")
        self.assertTrue(r["items"]) ; self.assertTrue(all(x["customer_id"] in APP.public_ids for x in r["items"]))
        outside = next(c for c, v in APP.store.customers.items() if v["region"] != "서울" and c not in {d["customer_id"] for d in S.DEMO})
        self.assertEqual(pub_call("GET", f"/api/customers/{outside}/summary")[0], 404)
        self.assertEqual(pub_call("POST", "/api/recommend", {"customer_id": outside})[0], 404)
        self.assertEqual(pub_call("POST", "/api/recommend", {"customer_id": "C0698", "housing": "first", "use_jeonse": True})[0], 200)
    def test_local_meta_not_public(self):
        self.assertFalse(call("GET", "/api/meta")[1]["public"])
    def test_rate_limit(self):
        codes = [pub_call("POST", "/api/apt/view", {"apt_id": "0000000000", "voter": "z" * 20})[0] for _ in range(S.LIMITS["view"] + 3)]
        self.assertIn(429, codes)
    def test_rate_limit_not_bypassed_by_spoofed_xff(self):
        H = S.make_handler(APP)
        def hit(xff):
            req = (f"POST /api/apt/view HTTP/1.1\r\nHost: x\r\nX-HF-Origin: {APP.secret}\r\nX-Forwarded-For: {xff}, 198.51.100.7\r\n"
                   f"Content-Length: 2\r\n\r\n{{}}").encode()
            out = io.BytesIO()
            class Srv: pass
            h = H.__new__(H); h.rfile = io.BytesIO(req); h.wfile = out; h.client_address = ("10.0.0.6", 0); h.server = Srv()
            h.handle_one_request()
            return int(out.getvalue().split()[1])
        codes = [hit(f"1.2.3.{i}") for i in range(S.LIMITS["view"] + 3)]   # 왼쪽 값을 매번 바꿔도
        self.assertIn(429, codes)

if __name__ == "__main__":
    unittest.main(verbosity=1)
