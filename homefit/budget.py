"""마이데이터(가명) 고객 재무 프로필과 서울 주택 구입 가능 예산 계산.

- 입력: mydata/pseudo/*.csv (원 단위) → 내부는 전부 만원(float, 표시할 때 반올림).
- 응답에 name, rrn, mobile, account_no, card_no, account_id 를 넣지 않는다.
- 정책 수치는 policy.json 에서 읽는다 (코드에 박지 않음).
"""
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PSEUDO_DIR = ROOT.parent.parent / "mydata" / "pseudo"
POLICY_PATH = ROOT / "policy.json"
CID_RE = re.compile(r"^C\d{4}$")
WON = 10000.0


def load_policy(path=POLICY_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read(name, pseudo_dir):
    with open(Path(pseudo_dir) / name, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def pmt(principal, annual_rate_pct, months):
    """원리금균등 월 상환액."""
    i = annual_rate_pct / 100 / 12
    if months <= 0:
        return 0.0
    if i == 0:
        return principal / months
    return principal * i / (1 - (1 + i) ** -months)


def pv(payment, annual_rate_pct, months):
    """월 상환액 payment 로 감당 가능한 원금 (pmt 의 역함수)."""
    i = annual_rate_pct / 100 / 12
    if payment <= 0 or months <= 0:
        return 0.0
    if i == 0:
        return payment * months
    return payment * (1 - (1 + i) ** -months) / i


def annual_service(loan_type, balance, rate_pct, assumptions):
    a = assumptions.get(loan_type, {"method": "credit5"})
    if a["method"] == "amortize":
        return 12 * pmt(balance, rate_pct, a["months"])
    if a["method"] == "interest_only":
        return balance * rate_pct / 100
    return balance / 5 + balance * rate_pct / 100          # credit5


class CustomerStore:
    """CSV 를 한 번 읽어 고객별로 묶어 둔다."""

    def __init__(self, pseudo_dir=PSEUDO_DIR, policy=None):
        self.policy = policy or load_policy()
        d = pseudo_dir
        self.customers = {r["customer_id"]: r for r in _read("customers.csv", d)}
        self.by = defaultdict(lambda: defaultdict(list))
        for name in ("bank_accounts", "securities", "pensions", "insurance", "loans"):
            for r in _read(f"{name}.csv", d):
                self.by[r["customer_id"]][name].append(r)
        self.salary = defaultdict(lambda: {"sum": 0.0, "months": set()})
        for r in _read("bank_transactions.csv", d):
            if r["tx_type"] == "IN" and r["description"] == "급여":
                s = self.salary[r["customer_id"]]
                s["sum"] += int(r["amount"]) / WON
                s["months"].add(r["tx_date"][:6])
        self.card = defaultdict(lambda: {"sum": 0.0, "months": set()})
        for r in _read("card_transactions.csv", d):
            c = self.card[r["customer_id"]]
            c["sum"] += int(r["amount"]) / WON
            c["months"].add(r["tx_date"][:6])

    def list(self, q="", region=None, limit=50):
        items = []
        for cid, c in self.customers.items():
            if q and not cid.startswith(q.upper()):
                continue
            if region and c["region"] != region:
                continue
            items.append(public_customer(c))
        return {"items": items[:limit], "total": len(items)}

    def profile(self, cid):
        if not CID_RE.match(cid or "") or cid not in self.customers:
            return None
        return build_profile(self.customers[cid], self.by[cid], self.salary.get(cid), self.card.get(cid), self.policy)


def public_customer(c):
    return {"customer_id": c["customer_id"], "age": int(c["age"]),
            "region": c["region"], "income_band": int(c["income_band"])}


def build_profile(cust, rel, salary, card, policy):
    p = policy["profile"]
    band = int(cust["income_band"])
    flags = []
    # 연소득: 최근 12개월 급여 입금 합. 없으면 소득구간 중앙값으로 대체
    if salary and salary["months"]:
        n = len(salary["months"])
        income = salary["sum"] if n >= 12 else salary["sum"] / n * 12
        income_method = "salary_tx"
    else:
        income = 12 * (180 + 45 * band) * 1.111
        income_method = "income_band"
        n = 0
        flags.append("급여 입금 기록이 없어 소득 구간의 평균값으로 연소득을 어림했어요.")
    accts = defaultdict(float)
    for a in rel["bank_accounts"]:
        accts[a["account_type"]] += int(a["balance"]) / WON
    cash = sum(accts.values())
    sec = sum(int(s["market_value"]) / WON for s in rel["securities"])
    pens = defaultdict(float)
    for x in rel["pensions"]:
        pens[x["pension_type"]] += int(x["balance"]) / WON
    ins_m = sum(int(x["monthly_premium"]) / WON for x in rel["insurance"] if x["status"] == "유지")
    card_m = card["sum"] / max(len(card["months"]), 1) if card else 0.0
    reserve = p["emergency_months"] * (card_m + ins_m)
    debts, credit_bal, service, jeonse_service = [], 0.0, 0.0, 0.0
    for ln in rel["loans"]:
        bal = int(ln["balance"]) / WON
        rate = int(ln["rate_bp"]) / 100
        a = annual_service(ln["loan_type"], bal, rate, p["loan_assumptions"])
        service += a
        if ln["loan_type"] == "신용":
            credit_bal += bal
        if ln["loan_type"] == "전세자금":
            jeonse_service += a
        debts.append({"type": ln["loan_type"], "balance": round(bal), "rate": rate,
                      "annual_service": round(a),
                      "assumption": p["loan_assumptions"].get(ln["loan_type"], {}).get("label", "")})
    types = {d["type"] for d in debts}
    if "주택담보" in types:
        housing, housing_note = "one_cond", "주택담보대출이 있어 집을 한 채 가진 것으로 보고, 지금 집을 팔고 옮기는 경우로 계산했어요. 다르면 2단계에서 바꿔 주세요."
    elif "전세자금" in types:
        housing, housing_note = "none", "전세자금대출이 있어 집이 없는 세입자로 보고 계산했어요. 다르면 2단계에서 바꿔 주세요."
    else:
        housing, housing_note = "none", "가진 집 정보가 없어 무주택으로 보고 계산했어요. 다르면 2단계에서 바꿔 주세요."
    equity = max(0.0, cash + p["securities_haircut"] * sec - credit_bal - reserve)
    jeonse_bal = sum(d["balance"] for d in debts if d["type"] == "전세자금")
    # 전세대출은 보증금의 최대 80% → 보증금 ≥ 잔액/0.8, 대출 상환 후 순반환 ≥ 잔액×0.25 (보수적 하한)
    jeonse_net = jeonse_bal * (1 / p.get("jeonse_loan_ratio", 0.8) - 1) if jeonse_bal else 0.0
    pension_extra = p["pension_dc_irp_haircut"] * (pens.get("DC", 0) + pens.get("IRP", 0))
    contrib_m = sum(int(x["monthly_contribution"]) / WON for x in rel["pensions"] if x["pension_type"] != "DB")
    surplus_m = income / 12 - card_m - ins_m - contrib_m - service / 12
    if income and service / income > 0.4:
        flags.append("지금 갚고 있는 대출이 이미 소득의 40%를 넘어 새 대출을 받기 어려워요.")
    if income_method == "salary_tx":
        flags.append("연소득은 최근 12개월 급여 입금액을 더해 계산했어요(세전·세후 구분 없음).")
    return {
        **public_customer(cust),
        "income": {"annual": round(income), "method": income_method, "months_observed": n},
        "assets": {"cash": round(cash), "by_account_type": {k: round(v) for k, v in accts.items()},
                   "securities": round(sec), "securities_haircut": p["securities_haircut"],
                   "pensions": {k: round(v) for k, v in pens.items()},
                   "pension_usable_extra": round(pension_extra)},
        "spending": {"card_monthly": round(card_m), "insurance_monthly": round(ins_m),
                     "pension_contrib_monthly": round(contrib_m), "emergency_reserve": round(reserve)},
        "debts": debts,
        "debt_service_annual": round(service),
        "credit_deducted": round(credit_bal),
        "dsr_current": round(service / income, 4) if income else None,
        "equity": round(equity),
        "surplus_monthly": round(surplus_m),
        "inferred_housing": housing, "housing_note": housing_note,
        "jeonse_net_est": round(jeonse_net),
        "flags": flags,
        # 계산용 원값 (반올림 전)
        "_raw": {"income": income, "equity": equity, "service": service, "pension_extra": pension_extra,
                 "surplus_m": surplus_m, "jeonse_net": jeonse_net, "jeonse_service": jeonse_service},
    }


# ---------- 예산 ----------

def loan_cap(price, policy):
    for t in policy["loan_cap_by_price"]:
        if t["max_price"] is None or price <= t["max_price"]:
            return t["cap"], t["label"]
    return 0, "-"


def acq_tax(price, housing, large, policy):
    """취득세 + 지방교육세 + 농어촌특별세(85㎡ 초과). 금액 만원."""
    if housing in ("multi", "multi3"):
        rate = policy["acq_tax_multi_rate"][housing]
        edu = 0.004
        rural = (0.006 if housing == "multi" else 0.010) if large else 0.0
    else:
        eok = price / 10000
        rate = 0.01 if price <= 60000 else (0.03 if price > 90000 else (eok * 2 / 3 - 3) / 100)
        edu = rate * 0.1
        rural = 0.002 if large else 0.0
    return price * (rate + edu + rural), rate


def brokerage(price, policy):
    for t in policy["brokerage"]:
        if t["max_price"] is None or price < t["max_price"]:
            fee = price * t["rate"]
            if t["cap"] is not None:
                fee = min(fee, t["cap"])
            return fee * (1 + policy["brokerage_vat"])
    return 0.0


def costs(price, housing, large, policy):
    tax, rate = acq_tax(price, housing, large, policy)
    brk = brokerage(price, policy)
    o = policy["other_costs"]
    stamp = 0 if price <= 10000 else (o["stamp_10eok_below"] if price <= 100000 else o["stamp_10eok_over"])
    other = stamp + price * o["bond_discount_rate"] + o["legal_fee"]
    return {"acq_tax": tax, "acq_tax_rate": rate, "brokerage": brk, "other": other, "total": tax + brk + other}


def dsr_loan_limit(income, existing_service, rate_pct, stress_pp, dsr_limit, term_years):
    room = income * dsr_limit - existing_service
    return pv(room / 12, rate_pct + stress_pp, term_years * 12), room


def loan_at(price, housing, dsr_limit_amt, policy):
    ltv = policy["ltv"][housing]
    cap, tier = loan_cap(price, policy)
    parts = {"ltv": ltv * price, "price_cap": cap, "dsr": dsr_limit_amt}
    amount = max(0.0, min(parts.values()))
    binding = min(parts, key=parts.get) if ltv > 0 else "none_loan"
    return amount, parts, binding, tier


def compute_budget(profile, policy, housing=None, rate=None, term_years=None, include_costs=True,
                   use_pension=False, large=False, purpose="live", extra=0.0, use_jeonse=False):
    raw = profile["_raw"]
    housing = housing or profile["inferred_housing"]
    if housing not in policy["ltv"]:
        raise ValueError("housing")
    rate = policy["base_rate_pct"] if rate is None else float(rate)
    term_years = term_years or policy["term_years"]
    pension_add = raw["pension_extra"] if use_pension and housing not in ("one_cond", "multi") else 0.0
    jeonse_add = raw.get("jeonse_net", 0.0) if use_jeonse else 0.0
    extra = max(0.0, float(extra or 0))
    equity = raw["equity"] + pension_add + jeonse_add + extra
    # 전세보증금으로 전세대출을 갚는다고 보면 그 이자는 DSR·월 잉여에서 빠진다
    repaid = raw.get("jeonse_service", 0.0) if jeonse_add else 0.0
    service = raw["service"] - repaid
    surplus_m = raw["surplus_m"] + repaid / 12
    dsr_amt, room = dsr_loan_limit(raw["income"], service, rate, policy["stress_rate_pp"],
                                   policy["dsr_limit"], term_years)

    def feasible(p):
        loan, parts, binding, tier = loan_at(p, housing, dsr_amt, policy)
        c = costs(p, housing, large, policy)["total"] if include_costs else 0.0
        return p + c <= equity + loan + 1e-9

    best, step, p = 0, 100, 100            # 100만원 단위 탐색 (가격구간 경계에서 한도가 끊겨도 정확)
    upper = equity + max(v["cap"] for v in policy["loan_cap_by_price"]) + 100
    while p <= upper:
        if feasible(p):
            best = p
        p += step
    loan, parts, binding, tier = loan_at(best, housing, dsr_amt, policy) if best else (0.0, {"ltv": 0, "price_cap": 0, "dsr": dsr_amt}, "equity", "-")
    c = costs(best, housing, large, policy) if (best and include_costs) else {"acq_tax": 0, "acq_tax_rate": 0, "brokerage": 0, "other": 0, "total": 0}
    need = best + c["total"] - equity
    loan_used = max(0.0, min(loan, need))
    if best and loan - loan_used > 200 and loan_cap(best + step, policy)[0] < loan_cap(best, policy)[0]:
        binding = "price_tier"            # 한 단계 위 가격구간으로 가면 상한이 줄어 더 비싼 집을 못 사는 경우
    elif best and loan - loan_used > 200:
        binding = "cost_step"             # 중개보수 등 부대비용 요율이 계단식으로 오르는 경계
    monthly = pmt(loan_used, rate, term_years * 12)
    new_dsr = (service + 12 * monthly) / raw["income"] if raw["income"] else None
    warnings = []
    if housing in ("multi", "multi3"):
        warnings.append("집을 한 채 더 사는 경우 서울에서는 주택담보대출을 받을 수 없고, 취득세도 크게 올라가요.")
    if housing == "one_cond":
        warnings.append("집이 있으면 지금 집을 6개월 안에 판다는 조건으로만 대출을 받을 수 있어요. 지금 집을 판 돈은 넣지 않았으니 '추가 자금'에 넣어 보세요.")
    if room <= 0:
        warnings.append("지금 갚고 있는 대출만으로 소득의 40%를 넘어 새 대출을 받을 수 없어요.")
    if purpose == "invest" and policy.get("toheo_all_seoul"):
        warnings.append("서울 아파트는 토지거래허가구역이라 산 뒤 2년은 직접 살아야 해요. 전세를 끼고 사는 방식은 어려워요.")
    if use_jeonse and jeonse_add:
        warnings.append("돌려받을 전세보증금은 전세대출 잔액으로 어림한 최소 금액이에요. 실제로 더 받는다면 그 차액을 '추가 자금'에 넣어 보세요.")
    if use_pension and pension_add:
        warnings.append("퇴직연금(DC·IRP)을 집 사는 데 미리 꺼내 쓴다고 계산했어요. 세금이 붙고 노후 자금이 줄어드니 신중히 판단하세요.")
    elif use_pension:
        warnings.append("집이 있으면 집 사는 이유로 연금을 미리 꺼낼 수 없어 연금은 넣지 않았어요.")
    if repaid:
        warnings.append("전세보증금을 돌려받아 전세대출을 갚는다고 보고, 그 대출 이자는 계산에서 뺐어요.")
    def equity_needed(price):
        ln, _, _, _ = loan_at(price, housing, dsr_amt, policy)
        cc = costs(price, housing, large, policy)["total"] if include_costs else 0.0
        return max(0.0, price + cc - ln)
    out = {
        "_equity_needed": equity_needed,
        "_surplus_m": surplus_m,
        "budget_max": round(best),
        "equity_used": round(equity),
        "equity_parts": {"base": round(raw["equity"]), "pension": round(pension_add), "jeonse": round(jeonse_add), "extra": round(extra)},
        "housing": housing,
        "rate": rate, "term_years": term_years,
        "loan": {"amount": round(loan_used), "max_at_price": round(loan), "binding": binding,
                 "limits": {"ltv": {"value": round(parts["ltv"]), "ratio": policy["ltv"][housing]},
                            "dsr": {"value": round(parts["dsr"]), "stress_rate": round(rate + policy["stress_rate_pp"], 2),
                                    "dsr_cap": policy["dsr_limit"], "room_annual": round(room)},
                            "price_cap": {"value": round(parts["price_cap"]), "tier": tier}}},
        "costs": {k: (round(v) if k != "acq_tax_rate" else round(v, 4)) for k, v in c.items()},
        "monthly_payment": round(monthly),
        "dsr_after": round(new_dsr, 4) if new_dsr is not None else None,
        "surplus_after": round(surplus_m - monthly),
        "warnings": warnings,
    }
    return out


def budget_with_sensitivity(profile, policy, **kw):
    base = compute_budget(profile, policy, **kw)
    r = base["rate"]
    kw.pop("rate", None)
    kw2 = {k: v for k, v in kw.items() if k != "rate"}
    lo = compute_budget(profile, policy, rate=max(0.5, r - 1), **kw2)["budget_max"]  # noqa
    hi = compute_budget(profile, policy, rate=r + 1, **kw2)["budget_max"]
    base["sensitivity"] = {"rate_minus_1pp": lo - base["budget_max"], "rate_plus_1pp": hi - base["budget_max"]}
    return base
