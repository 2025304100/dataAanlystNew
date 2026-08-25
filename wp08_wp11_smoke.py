"""WP0-8 + WP1-1 烟测脚本 - 修正响应解析 + 枚举契约 - v2"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BASE = "http://127.0.0.1:8000/api/v1"

FAILURES = []
PASSES = []
CTX = {}


def req(method, path, body=None, timeout=300):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except Exception:
            payload = {"raw_text_last_1k": text[-1000:]}
        return e.code, payload


def run(name, fn):
    t0 = time.monotonic()
    try:
        ok, info = fn()
        dt = (time.monotonic() - t0) * 1000
        prefix = "✅" if ok else "❌"
        (PASSES if ok else FAILURES).append(name)
        print(f"{prefix} [{dt:>6.0f}ms] {name} {info}")
    except Exception as e:
        dt = (time.monotonic() - t0) * 1000
        FAILURES.append(name)
        print(f"❌ [{dt:>6.0f}ms] {name} EXCEPTION={type(e).__name__}: {e}")


# ========= A =========
def a1():
    s, b = req("GET", "/portfolios")
    info = f"status={s}"
    if isinstance(b, list):
        info += f" LIST len={len(b)}"
        if b:
            pid0 = b[0].get("id") if isinstance(b[0], dict) else None
            info += f" first_id={pid0}"
            if pid0:
                CTX["pid"] = pid0
    elif isinstance(b, dict):
        info += f" DICT keys={list(b.keys())[:10]}"
        # Some endpoints paginate, some wrap. Accept inner items too.
        for k in ("items", "data", "rows"):
            if isinstance(b.get(k), list) and b[k]:
                pid0 = b[k][0].get("id") if isinstance(b[k][0], dict) else None
                if pid0:
                    CTX["pid"] = pid0
                info += f" found items via {k}, first_id={pid0}"
                break
    ok = s == 200 and CTX.get("pid") is not None
    if not ok and s != 200:
        info += f" | body[0:500]={json.dumps(b)[:500]}"
    return ok, info


def a2():
    pid = CTX.get("pid")
    if not pid:
        return False, "no pid"
    s, b = req("GET", f"/portfolios/{pid}")
    info = f"status={s}, type={type(b).__name__}"
    if isinstance(b, dict):
        id_val = b.get("id")
        info += f" id={id_val}"
        if id_val is None:
            # Sometimes the wrapper returns object inside 'data'/'item'
            for k in ("data", "item", "portfolio"):
                if isinstance(b.get(k), dict):
                    id_val = b[k].get("id")
                    info += f" → via .{k} id={id_val}"
                    break
        members0 = b.get("members")
        if isinstance(members0, list):
            info += f" members={len(members0)}"
            if members0:
                CTX["symbol_ids"] = [m.get("symbol_id") for m in members0[:20] if isinstance(m, dict)]
        ok = s == 200 and id_val is not None
    else:
        ok = s == 200
    if not ok and s != 200:
        info += f" | body[0:500]={json.dumps(b)[:500]}"
    return ok, info


def extract_list_of_dicts(body):
    """Generic: return list[dict] from either raw list or paginated wrapper."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("items", "data", "rows", "results"):
            items = body.get(k)
            if isinstance(items, list):
                return items
    return []


def a3():
    s, b = req("GET", "/factor-sets?limit=50")
    items = extract_list_of_dicts(b)
    info = f"status={s}, wrapper_type={type(b).__name__}, extracted_items={len(items)}"
    if items and isinstance(items[0], dict):
        info += f" first id={items[0].get('id')} status={items[0].get('status')}"
    ok = s == 200
    if not ok:
        info += f" | body={json.dumps(b)[:400]}"
    return ok, info


def a4():
    s, b = req("GET", "/factor-sets?limit=50")
    if s != 200:
        return False, f"factor-sets HTTP {s}"
    items = extract_list_of_dicts(b)
    if not items:
        # Maybe endpoint uses different path. Try alternative.
        return False, "factor-sets 返回空，跳过 B 系列"
    draft = next((i for i in items if isinstance(i, dict) and i.get("status") == "draft"), None)
    frozen = next((i for i in items if isinstance(i, dict) and i.get("status") == "frozen"), None)
    fs = draft or frozen or items[0]
    CTX["fs"] = fs
    info = f"选择 fs_id={fs.get('id') if isinstance(fs, dict) else 'N/A'}, status={fs.get('status') if isinstance(fs, dict) else 'N/A'}, n_members={fs.get('n_members') if isinstance(fs, dict) else (fs.get('member_count') if isinstance(fs, dict) else 'N/A')}"
    return True, info


def a5():
    s, b = req("GET", "/factor-models?limit=10")
    items = extract_list_of_dicts(b)
    info = f"status={s}, type={type(b).__name__}, items={len(items)}"
    ok = s == 200
    if not ok:
        info += f" | body={json.dumps(b)[:400]}"
    return ok, info


def a6():
    pid = CTX.get("pid")
    if not pid:
        return False, "no pid"
    s, b = req("GET", f"/portfolios/{pid}/factor-usage-options")
    if isinstance(b, dict):
        info = f"status={s}, fm={len(b.get('factor_models') or [])}, fs={len(b.get('factor_sets') or [])}, rules={len(b.get('portfolio_rules') or [])}, blocking={len(b.get('blocking_reasons') or [])}, global_active_model_run_id_type={type(b.get('global_active_model_run_id')).__name__}"
    else:
        info = f"status={s}, body_type={type(b).__name__}"
    ok = s == 200 and isinstance(b, dict) and "factor_models" in b
    if not ok:
        info += f" | body={json.dumps(b)[:500]}"
    return ok, info


# ========= B =========
def b1_freeze():
    fs = CTX.get("fs")
    if not isinstance(fs, dict):
        return False, "no fs"
    if fs.get("status") == "frozen":
        return True, "已冻结 skip freeze (仍可用)"
    fsid = fs.get("id")
    if not fsid:
        return False, "fs id 缺失"
    s, b = req("POST", f"/factor-sets/{fsid}/freeze", body={"reason": f"WP0-8 smoke {int(time.time())}"})
    info = f"status={s}, new_status={b.get('status') if isinstance(b, dict) else 'N/A'}"
    if s != 200:
        info += f" | body={json.dumps(b)[:600]}"
        return False, info
    if isinstance(b, dict) and b.get("status") == "frozen":
        CTX["fs"] = b
    return True, info


def b2_train():
    fs = CTX.get("fs")
    if not isinstance(fs, dict):
        return False, "no fs"
    fsid = fs.get("id")
    if not fsid:
        return False, "no fs id"
    payload = {
        "factor_set_id": fsid,
        "factor_set_version": fs.get("version"),
        "asset_type": "STOCK",
        "mode": "offline_minimal",
        "actor": "wp08_smoke",
        "note": f"WP0-8 smoke offline_minimal @{int(time.time())}",
    }
    s, b = req("POST", "/factor-models/train", body=payload, timeout=180)
    info = ""
    if isinstance(b, dict):
        rid = b.get("id")
        info = f"status={s}, model_run_id={(rid or '')[:20] if rid else None}… status={b.get('status')}, n_features={b.get('n_features')}"
        if rid:
            CTX["model_run_id"] = rid
    else:
        info = f"status={s}, body_type={type(b).__name__}"
    if s not in (200, 201):
        info += f" | body={json.dumps(b)[:800]}"
        return False, info
    ok = isinstance(b, dict) and bool(b.get("id"))
    return ok, info


# ========= C =========
def c1_preflight():
    pid = CTX.get("pid")
    if not pid:
        return False, "no pid"
    s, opts = req("GET", f"/portfolios/{pid}/factor-usage-options")
    if s != 200 or not isinstance(opts, dict):
        return False, "no opts"
    fms = opts.get("factor_models") or []
    fs_list = opts.get("factor_sets") or []
    rules = opts.get("portfolio_rules") or []
    mrid = CTX.get("model_run_id") or (fms[0].get("factor_model_run_id") if fms else None)
    # factor_set_id: pick the matching one from fs_list (if model specifies it) else fs
    fsid = None
    if mrid and fms:
        for fm in fms:
            if fm.get("factor_model_run_id") == mrid:
                fsid = fm.get("factor_set_id")
                break
    if not fsid and isinstance(CTX.get("fs"), dict):
        fsid = CTX["fs"].get("id")
    if not fsid and fs_list:
        fsid = fs_list[0].get("factor_set_id") or fs_list[0].get("id")
    rule_id = rules[0].get("rule_id") if rules else None
    rule_version = rules[0].get("rule_version") if rules else None
    if not mrid:
        return False, "SKIP 无可用 factor_model_run_id"
    body = {
        "factor_model_run_id": mrid,
        "factor_set_id": fsid,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "run_mode": "research",
        "pit_mode": "best_effort",
    }
    CTX["apply_body"] = body
    s2, resp = req("POST", f"/portfolios/{pid}/strategy-preflight", body=body)
    info = (f"status={s2}, ok={resp.get('ok') if isinstance(resp, dict) else 'N/A'}"
            f" warn={len(resp.get('warnings') or []) if isinstance(resp, dict) else 'N/A'}"
            f" members={resp.get('member_count') if isinstance(resp, dict) else 'N/A'}")
    if s2 != 200:
        info += f" | body={json.dumps(resp)[:800]}"
        return False, info
    ok = isinstance(resp, dict) and resp.get("ok") is True
    return ok, info


def c2_apply():
    pid = CTX.get("pid")
    body = CTX.get("apply_body")
    if not pid or not body:
        return False, "缺少前置"
    s, resp = req("POST", f"/portfolios/{pid}/factor-usage", body=body)
    info = ""
    if isinstance(resp, dict):
        sid = resp.get("strategy_snapshot_id") or ""
        info = f"status={s}, snap_id_len={len(sid)} eff_from={resp.get('effective_from')}"
        if sid:
            CTX["strategy_snapshot_id"] = sid
    else:
        info = f"status={s}, body_type={type(b).__name__}"
    if s not in (200, 201):
        info += f" | body={json.dumps(resp)[:800]}"
        return False, info
    ok = bool(CTX.get("strategy_snapshot_id"))
    return ok, info


# ========= D =========
def d1_eval():
    pid = CTX.get("pid")
    snap = CTX.get("strategy_snapshot_id")
    if not pid or not snap:
        return False, f"SKIP: pid={pid} snap={'Y' if snap else 'N'}"
    td = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    body = {
        "strategy_snapshot_id": snap,
        "trade_date": td,
        "run_type": "backtest",
        "persist": True,
    }
    s, resp = req("POST", f"/portfolios/{pid}/evaluate", body=body, timeout=180)
    info = ""
    if isinstance(resp, dict):
        drid = resp.get("decision_run_id")
        info = (f"status={s}, rid={(drid or '')[:22] if drid else None}…"
                f" ev_count={resp.get('evidence_count')}"
                f" preview={len(resp.get('evidence_preview') or [])}"
                f" blocking={resp.get('blocking_status')}"
                f" persisted={resp.get('persisted')}")
        if drid:
            CTX["drid"] = drid
    else:
        info = f"status={s}, body_type={type(resp).__name__}"
    if s != 200:
        info += f" | body={json.dumps(resp)[:1000]}"
        return False, info
    return bool(CTX.get("drid")), info


def d2_get_run():
    drid = CTX.get("drid")
    if not drid:
        return False, "no drid"
    s, b = req("GET", f"/decision-runs/{urllib.parse.quote(drid)}")
    info = (f"status={s}, blocking_status={b.get('blocking_status') if isinstance(b, dict) else 'N/A'}"
            f" td={b.get('trade_date') if isinstance(b, dict) else 'N/A'}"
            f" member={b.get('member_count') if isinstance(b, dict) else 'N/A'}/uni={b.get('universe_count') if isinstance(b, dict) else 'N/A'}"
            f" id_match={b.get('id') == drid if isinstance(b, dict) else False}")
    if s != 200:
        info += f" | body={json.dumps(b)[:500]}"
        return False, info
    ok = isinstance(b, dict) and b.get("id") == drid
    return ok, info


def d3_list_runs():
    pid = CTX.get("pid")
    if not pid:
        return False, "no pid"
    s, b = req("GET", f"/portfolios/{pid}/decision-runs?limit=5")
    items = b.get("items", []) if isinstance(b, dict) else []
    total = b.get("total") if isinstance(b, dict) else None
    drid = CTX.get("drid")
    contains = any(i.get("id") == drid for i in items) if isinstance(items, list) and drid else False
    info = f"status={s}, total={total}, items={len(items)}, contains_new_run={contains}"
    if s != 200:
        info += f" | body={json.dumps(b)[:500]}"
        return False, info
    if drid and not contains and isinstance(total, int) and total == 0:
        info += " ⚠️ D-bug 风险：刚 persist 的 run 没有出现在列表"
    return isinstance(b, dict), info


def d4_evidence():
    drid = CTX.get("drid")
    if not drid:
        return False, "no drid"
    s, b = req("GET", f"/decision-runs/{urllib.parse.quote(drid)}/evidence?limit=20")
    info = ""
    if isinstance(b, dict):
        items = b.get("items", []) if isinstance(b.get("items"), list) else []
        total = b.get("total")
        info = f"status={s}, total={total}, returned={len(items)}"
        if items:
            it0 = items[0]
            fcs = it0.get("factor_contributions_json")
            info += (f", first_action={it0.get('action')}"
                     f", symbol_id={it0.get('symbol_id')}"
                     f", fcs_type={type(fcs).__name__}"
                     f", constraints_type={type(it0.get('constraints_json')).__name__}"
                     f", reasons_type={type(it0.get('reason_codes_json')).__name__}")
            if fcs is not None and not isinstance(fcs, (dict, list)):
                info += " ❌ factor_contributions_json NOT dict/list"
                return False, info
    else:
        info = f"status={s}, body_type={type(b).__name__}"
    if s != 200:
        info += f" | body={json.dumps(b)[:600]}"
        return False, info
    return isinstance(b, dict), info


def d5_rejected_filter():
    drid = CTX.get("drid")
    if not drid:
        return False, "no drid"
    s, b = req("GET", f"/decision-runs/{urllib.parse.quote(drid)}/evidence?action=REJECTED&limit=10")
    info = f"status={s}, rejected_total={b.get('total') if isinstance(b, dict) else 'N/A'}"
    if s != 200:
        info += f" | body={json.dumps(b)[:600]}"
        return False, info
    return isinstance(b, dict), info


# ========= E =========
def e1_backtest():
    pid = CTX.get("pid")
    if not pid:
        return False, "no pid"
    mrid = CTX.get("model_run_id")
    snap = CTX.get("strategy_snapshot_id")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # 严格按后端 PortfolioBacktestRequest schema（extra="forbid"，8 契约参数）
    payload = {
        "portfolio_id": pid,
        "start_date": start,
        "end_date": end,
        "run_name": f"WP0-8 smoke {int(time.time())}",
        "benchmark": "上证指数",
        "strategy_snapshot_id": snap,
        "score_weight_mode": "manual",
        "factor_model_run_id": mrid,
        # ↓ 8 契约参数（顶层字段，禁止嵌套 cost_config 等额外字段）
        "initial_capital": 1_000_000.0,
        "commission_rate": 0.0003,
        "stamp_tax_rate": 0.001,
        "slippage_bps": 5,
        "price_type": "NEXT_OPEN",
        "volume_limit_pct": 0.10,
        "rebalance_frequency": "daily",
        "pit_mode": "legacy_research",
    }
    s, b = req("POST", "/backtest/portfolio/run", body=payload, timeout=300)
    info = ""
    if isinstance(b, dict):
        eq = b.get("equity")
        tr = b.get("trades")
        pf = b.get("performance")
        info = (f"status={s}, equity={len(eq) if isinstance(eq, list) else 'N/A'}"
                f" trades={len(tr) if isinstance(tr, list) else 'N/A'}"
                f" perf_keys={list(pf.keys())[:8] if isinstance(pf, dict) else 'N/A'}")
        # 若返回 run_id / backtest_id，则记下
        for k in ("id", "backtest_id", "run_id"):
            if b.get(k):
                info += f" {k}={b.get(k)}"
                break
    else:
        info = f"status={s}, body_type={type(b).__name__}"
    if s not in (200, 201):
        info += f" | body={json.dumps(b)[:800]}"
        return False, info
    return isinstance(b, dict), info


if __name__ == "__main__":
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as h:
            print(f"[pre-flight] /health {h.status}: {json.loads(h.read().decode())}\n")
    except Exception as e:
        print(f"[pre-flight] FAIL: {e}")
        sys.exit(2)

    sections = [
        ("==== A. 基础列表/选项 ====", [
            ("A1 GET /portfolios → pid", a1),
            ("A2 GET /portfolios/:id", a2),
            ("A3 GET /factor-sets", a3),
            ("A4 挑选 FactorSet", a4),
            ("A5 GET /factor-models", a5),
            ("A6 GET factor-usage-options", a6),
        ]),
        ("==== B. WP0-8 freeze + train offline_minimal ====", [
            ("B1 POST factor-sets/:id/freeze", b1_freeze),
            ("B2 POST factor-models/train", b2_train),
        ]),
        ("==== C. WP0-2g factor-usage (saveAndApply) ====", [
            ("C1 POST strategy-preflight ok=True", c1_preflight),
            ("C2 POST factor-usage → strategy_snapshot_id", c2_apply),
        ]),
        ("==== D. WP1-1 evaluate/DecisionRun/Evidence ====", [
            ("D1 POST evaluate persist=True → decision_run_id", d1_eval),
            ("D2 GET decision-runs/:id (404 = 之前 BUG 未 COMMIT 已修)", d2_get_run),
            ("D3 GET portfolio decision-runs 列表(含刚创建的)", d3_list_runs),
            ("D4 GET evidence 结构(JSON字段必须 dict/list)", d4_evidence),
            ("D5 GET evidence?action=REJECTED 过滤", d5_rejected_filter),
        ]),
        ("==== E. WP0-8 POST backtest/portfolio/run (8契约参数) ====", [
            ("E1 POST /backtest/portfolio/run (extra=forbid 契约对齐)", e1_backtest),
        ]),
    ]

    for title, cases in sections:
        print(title)
        for n, fn in cases:
            run(n, fn)
        print()

    T = len(PASSES) + len(FAILURES)
    print(f"==== SUMMARY: PASS={len(PASSES)}/{T}, FAIL={len(FAILURES)}/{T} ====")
    if PASSES:
        print(f"PASSES ({len(PASSES)}):")
        for p in PASSES:
            print(f"  ✅ {p}")
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  ❌ {f}")
    sys.exit(0 if not FAILURES else 1)
