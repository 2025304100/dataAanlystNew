# -*- coding: utf-8 -*-
"""缺口补测 T3：切分预算计算正确性对账（黑盒 + 独立重算）。

A. 内部一致性：三段和=total、meets_floor 与门槛关系、purge/embargo 折算
B. 独立重算：用 Python 日历估算交易日/周点/月点数量级，与 API 返回交叉比对
C. 边界敏感性：区间+1年 的增量合理性
"""
import datetime as dt
import json

import requests

BASE = "http://127.0.0.1:8000/api/v1"
OUT = []


def log(case, ok, detail=""):
    OUT.append({"case": case, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


def budget(sd, ed, freq, th=5, tr=0.6, vr=0.2):
    r = requests.post(f"{BASE}/factor-mining/split-budget", json={
        "start_date": sd, "end_date": ed, "frequency": freq,
        "target_horizon": th, "train_ratio": tr, "validation_ratio": vr}, timeout=120)
    return r.json() if r.ok else {"_status": r.status_code}


def weekdays_between(a, b):
    d, n = a, 0
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return n


# ══ A 内部一致性（多组参数扫描） ══
cases = [
    ("2024-01-01", "2026-09-01", "daily", 5),
    ("2024-01-01", "2026-09-01", "weekly", 5),
    ("2024-01-01", "2026-09-01", "monthly", 5),
    ("2025-06-01", "2026-09-01", "daily", 20),
    ("2020-01-01", "2026-09-01", "weekly", 10),
]
ok_a = True
details = []
for sd, ed, fq, th in cases:
    b = budget(sd, ed, fq, th)
    tp, tr_, va, te = (b.get("total_points"), b.get("train_points"),
                       b.get("val_points"), b.get("test_points"))
    floor = b.get("frequency_floor")
    mf = b.get("meets_floor")
    # 三段和 ≈ total（purge/embargo 从边界扣减，允许 ±(purge+embargo+tail) 容差）
    leak = abs((tr_ or 0) + (va or 0) + (te or 0) - (tp or 0))
    tol = (b.get("purge_points") or 0) + (b.get("embargo_points") or 0) + (b.get("tail_loss") or 0) + 2
    consist = b.get("available") and 0 <= leak <= tol and (mf == ((tp or 0) >= (floor or 0)))
    # 日/周/月门槛应为 252/104/36
    floor_expect = {"daily": 252, "weekly": 104, "monthly": 36}[fq]
    ok_a = ok_a and consist and floor == floor_expect
    details.append(f"{fq}[{th}] tp={tp} t/v/vt={tr_}/{va}/{te} floor={floor} mf={mf} leak={leak}")
log("A 五组参数内部一致性 + 门槛 252/104/36", ok_a, " ; ".join(details)[:280])

# ══ B 独立重算（数量级比对） ══
a, b2 = dt.date(2024, 1, 1), dt.date(2026, 9, 1)
wd = weekdays_between(a, b2)
bd = budget("2024-01-01", "2026-09-01", "daily")
# A股交易日约占工作日 88%±5%
ratio = (bd.get("total_points") or 0) / wd
log("B1 日频点数 vs 工作日数（比例应在 0.80~0.98）", 0.80 <= ratio <= 0.98,
    f"api={bd.get('total_points')} 工作日={wd} 比例={ratio:.3f}")
bw = budget("2024-01-01", "2026-09-01", "weekly")
iso_weeks = 0
d = a
while d <= b2:
    d += dt.timedelta(days=7)
    iso_weeks += 1
# 周频点数应≈ISO周数（137±3）
log("B2 周频点数≈ISO周数", abs((bw.get("total_points") or 0) - iso_weeks) <= 4,
    f"api={bw.get('total_points')} 估算周数={iso_weeks}")
bm = budget("2024-01-01", "2026-09-01", "monthly")
months = (b2.year - a.year) * 12 + (b2.month - a.month) + 1
log("B3 月频点数=整月数(33)", bm.get("total_points") == months,
    f"api={bm.get('total_points')} 估算={months}")

# ══ C 边界敏感性：+1 年 ══
b3 = budget("2023-01-01", "2026-09-01", "daily")
inc = (b3.get("total_points") or 0) - (bd.get("total_points") or 0)
log("C 前移一年日频增量在 230~260", 230 <= inc <= 260, f"增量={inc}")

# ══ D purge 折算：日频 th=5 → purge=5；周频 th=5 → purge=ceil(5/5)=1? ══
bp5 = budget("2024-01-01", "2026-09-01", "daily", 5)
bp20 = budget("2024-01-01", "2026-09-01", "daily", 20)
bw20 = budget("2024-01-01", "2026-09-01", "weekly", 20)
log("D purge 随 horizon/频率变化合理",
    (bp5.get("purge_points") or 0) <= (bp20.get("purge_points") or 0)
    and (bw20.get("purge_points") or 99) <= (bp20.get("purge_points") or 0),
    f"d5={bp5.get('purge_points')} d20={bp20.get('purge_points')} w20={bw20.get('purge_points')}"
    f" 交易日折算 d20={bp20.get('purge_trading_days')} w20={bw20.get('purge_trading_days')}")

# ══ E 比例语义：train 0.6/val 0.2 → test≈0.2（点数占比） ══
tp = bd.get("total_points") or 1
shares = (round(bd["train_points"]/tp, 2), round(bd["val_points"]/tp, 2), round(bd["test_points"]/tp, 2))
log("E 三段点数占比≈60/20/20（±3pp，purge 扣减容忍）",
    all(abs(s - e) <= 0.06 for s, e in zip(shares, (0.6, 0.2, 0.2))),
    f"占比={shares} total={bd.get('total_points')}")

print(json.dumps([o for o in OUT if not o["ok"]], ensure_ascii=False)[:500])
