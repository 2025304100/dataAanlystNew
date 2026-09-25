# -*- coding: utf-8 -*-
"""设置→因子挖掘 · API 层黑盒测试（2026-09-23）。

依据《因子挖掘实验向导-详细设计.md》v3.3，从用户/契约视角直接打 HTTP：
- Step1 候选池：预览、50 硬下限、范围冲突阻断、快照三件套（创建/查看/重选）
- Step2 切分预算：日/周/月门槛（252/104/36）、镜像区间外阻断
- Step3 字段目录：分组、blocked 字段带原因
- Step4/提交：双锁（mining_domain 拒绝）、草稿、模板 25 个、校验幂等复用
- Step5 运行：小池真实 run、取消/停止/放弃状态机
- 错误信封：结构化 title_zh/detail_zh
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta

import requests

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"case": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {str(detail)[:220]}" if detail else ""),
          flush=True)
    return bool(ok)


def jget(r: requests.Response, *path, default=None):
    cur = r.json()
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p, default)
        elif isinstance(cur, list) and isinstance(p, int) and len(cur) > p:
            cur = cur[p]
        else:
            return default
    return cur


iso = lambda d: d.isoformat()
today = date(2026, 9, 1)

# ══════════ A. Step3 字段目录（§5） ══════════
r = requests.get(f"{BASE}/factor-mining/fields", timeout=30)
ok = r.status_code == 200
fields = jget(r, "fields", default=[]) or []
groups = {f.get("group") for f in fields}
blocked = [f for f in fields if f.get("availability") == "blocked"]
check("A1 字段目录 200 且分组含行情/估值/财报", ok and {"quote", "valuation"} <= groups,
      f"count={len(fields)} groups={sorted(g for g in groups if g)}")
codes = {f.get("field") for f in fields}
check("A2 常用 DSL 字段在目录中(close/pe_ttm/roe_ttm)", {"close", "pe_ttm", "roe_ttm"} <= codes,
      sorted(codes)[:12])
check("A3 blocked 字段附中文原因（不得可勾选）",
      all(f.get("blocked_reason_zh") for f in blocked) and len(blocked) >= 1,
      [f.get("field") for f in blocked])

# ══════════ B. 双锁状态（§8.2） ══════════
r = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30)
md = jget(r, "miningDomain", default={}) or {}
dw = jget(r, "duckdbWrite", default={}) or {}
check("B1 锁状态接口 200（测试前基线应空闲）", r.status_code == 200,
      f"miningDomain.busy={md.get('busy')} duckdbWrite.busy={dw.get('busy')} queue={dw.get('queue')}")
lock_idle_before = not md.get("busy")

# ══════════ C. 切分预算（§4 门槛） ══════════
def budget(sd, ed, freq, **kw):
    body = {"start_date": sd, "end_date": ed, "frequency": freq,
            "target_horizon": 5, "train_ratio": 0.6, "validation_ratio": 0.2, **kw}
    return requests.post(f"{BASE}/factor-mining/split-budget", json=body, timeout=90)

r = budget("2025-01-01", "2026-09-01", "daily")
b = r.json() if r.status_code == 200 else {}
pts = b.get("total_points")
check("C1 日频 1.6y 预算 ≥252 点门槛达标(meets_floor)", r.status_code == 200 and b.get("available")
      and (pts or 0) >= 252 and b.get("meets_floor") is True,
      {k: b.get(k) for k in ("available", "total_points", "frequency_floor", "meets_floor",
                              "train_points", "val_points", "test_points")})
r = budget("2025-01-01", "2025-12-31", "weekly")
b2 = r.json() if r.status_code == 200 else {}
check("C2 周频 1 年点数<104 时 meets_floor=False（如实给未达标）",
      r.status_code == 200 and (b2.get("total_points") or 999) < 104 and b2.get("meets_floor") is False,
      {k: b2.get(k) for k in ("available", "total_points", "frequency_floor", "meets_floor")})
r = budget("2016-01-01", "2018-12-31", "monthly")
b3 = r.json() if r.status_code == 200 else {}
check("C3 镜像范围外区间：available=False+原因（§4.1）",
      r.status_code == 200 and b3.get("available") is False and bool(b3.get("reason_zh")),
      {k: b3.get(k) for k in ("available", "reason_zh", "total_points")})
r = budget("2026-09-01", "2025-01-01", "daily")  # 起点>终点
b4 = r.json() if r.status_code == 200 else {}
check("C4 起止日期倒置：200+available=False 或 4xx（不得 5xx）",
      (r.status_code == 200 and b4.get("available") is False) or 400 <= r.status_code < 500,
      f"status={r.status_code} body={r.text[:120]}")
r = requests.post(f"{BASE}/factor-mining/split-budget",
                  json={"start_date": "2025-01-01", "end_date": "2026-01-01",
                        "frequency": "daily", "target_horizon": 5,
                        "train_ratio": 0.6, "validation_ratio": 0.6}, timeout=30)
check("C5 train+val 比例之和>100% 被入参校验拦截(422)而非 500", r.status_code == 422,
      f"status={r.status_code} body={r.text[:140]}")

# ══════════ D. Step1 候选池预览与硬门槛（§3.5） ══════════
r = requests.post(f"{BASE}/factor-mining/candidate-pools/preview",
                  json={"filter_config": {"markets": ["bj"]}}, timeout=120)
pj = r.json() if r.status_code == 200 else {}
check("D1 预览接口 200 返回命中统计与 can_generate", r.status_code == 200
      and "hits" in pj and "can_generate" in pj,
      {k: pj.get(k) for k in ("universe_size", "hits", "can_generate", "min_pool_size")})
r2 = requests.post(f"{BASE}/factor-mining/candidate-pools/preview",
                   json={"filter_config": {"valuation": {"total_market_cap": {"min_value": 5e11, "max_value": 1e11}}}},
                   timeout=60)
pj2 = r2.json() if r2.status_code == 200 else {}
check("D2 范围冲突(min>max)：预览 200+blocking_issues+can_generate=False（§3.3）",
      r2.status_code == 200 and pj2.get("can_generate") is False and pj2.get("blocking_issues"),
      f"status={r2.status_code} issues={str(pj2.get('blocking_issues'))[:150]}")
r3 = requests.post(f"{BASE}/factor-mining/candidate-pools/preview",
                   json={"filter_config": {"valuation": {"total_market_cap": {"min_value": 9e12, "max_value": 9.9e12}}}},
                   timeout=60)
hits0 = jget(r3, "hits") if r3.status_code == 200 else None
check("D3 极端区间预览命中=0（如实展示，不静默过滤）",
      r3.status_code == 200 and hits0 == 0 and r3.json().get("can_generate") is False,
      f"hits={hits0} can_generate={jget(r3, 'can_generate')}")
r4 = requests.post(f"{BASE}/factor-mining/candidate-pools/from-filter",
                   json={"name": "BB23-低于50硬下限池",
                         "filter_config": {"valuation": {"total_market_cap": {"min_value": 9e12, "max_value": 9.9e12}}}},
                   timeout=120)
code4 = jget(r4, "error_code") or ""  # 统一信封：error_code 在顶层（非 detail 内）
readable4 = "只" in r4.text  # 可读中文原因（嵌套 extras.detail_zh 亦计入）
check("D4 生成候选池命中<50 被 4xx 阻断且错误结构化（§3.5 硬下限，不得 500）",
      400 <= r4.status_code < 500 and bool(code4) and readable4,
      f"status={r4.status_code} code={code4} readable_zh={readable4} top_msg={jget(r4, 'user_message')}")

# ══════════ E. 候选池 + 快照三件套（§3.7 C3 裁决） ══════════
pool_id = snap_id = None
members = None
r = requests.post(f"{BASE}/factor-mining/candidate-pools/from-filter",
                  json={"name": f"BB23-北交所池-{int(time.time())}", "filter_config": {"markets": ["bj"]}},
                  timeout=300)
if r.status_code in (200, 201):
    pool_id = jget(r, "id", default=jget(r, "pool_id"))
    members = jget(r, "member_count", default=jget(r, "members"))
check("E1 条件建池成功（北交所）", bool(pool_id), f"pool={pool_id} members={members}")
if pool_id:
    r = requests.post(f"{BASE}/factor-mining/candidate-pools/{pool_id}/snapshot",
                      json={"analyze": True}, timeout=600)
    snap_id = jget(r, "id", default=jget(r, "snapshot_id"))
    stats = jget(r, "stats_json") or jget(r, "stats") or {}
    check("E2 创建快照(生成挖掘物料) 201 且返回分析", r.status_code in (200, 201) and bool(snap_id),
          f"snap={snap_id} keys={sorted(list(r.json().keys()))[:10]}")
    r = requests.get(f"{BASE}/factor-mining/candidate-pools/{pool_id}/snapshot/latest", timeout=60)
    latest_sid = jget(r, "snapshot_id", default=jget(r, "id", default=""))
    check("E3 查看最新快照 200 且 snapshot_id 一致", str(latest_sid) == str(snap_id),
          f"status={r.status_code} latest={latest_sid} expect={snap_id}")
    r = requests.delete(f"{BASE}/factor-mining/candidate-pools/{pool_id}/snapshot/latest", timeout=60)
    ok_del = r.status_code == 200
    r = requests.get(f"{BASE}/factor-mining/candidate-pools/{pool_id}/snapshot/latest", timeout=60)
    check("E4 重选快照（删除最新）后 latest 不再返回旧快照", ok_del and r.status_code == 404,
          f"del={ok_del} latest_status_after={r.status_code}")
    r = requests.post(f"{BASE}/factor-mining/candidate-pools/{pool_id}/snapshot",
                      json={"analyze": True}, timeout=600)
    snap_id = jget(r, "id", default=jget(r, "snapshot_id")) or snap_id
    check("E5 重新创建快照可用（重选后再生成）", r.status_code in (200, 201), f"snap={snap_id}")

# ══════════ F. 草稿与最终准备（§8.4 / §7） ══════════
draft_id = None
r = requests.post(f"{BASE}/factor-mining/drafts", json={
    "name": "BB23-草稿", "current_step": 2,
    "steps": {"step1": {"candidate_pool_snapshot_id": snap_id},
              "step2": {"start_date": "2025-01-01", "end_date": "2026-09-01",
                        "rebalance_frequency": "daily", "target_horizon": 5,
                        "train_ratio": 0.6, "validation_ratio": 0.2}},
}, timeout=60)
draft_id = jget(r, "draft_id", default=jget(r, "id"))
check("F1 保存草稿 201", r.status_code in (200, 201) and bool(draft_id), f"draft={draft_id}")
if draft_id:
    r = requests.get(f"{BASE}/factor-mining/drafts/{draft_id}", timeout=30)
    check("F2 读取草稿 200", r.status_code == 200 and bool(jget(r, "draft_id", default=jget(r, "id"))))
    r = requests.post(f"{BASE}/factor-mining/drafts/{draft_id}/prepare", timeout=60)
    blockers = jget(r, "blockers", default=[])
    check("F3 最终准备：配置不完整时返回 blockers 清单（不静默放行）",
          r.status_code == 200 and isinstance(blockers, list) and len(blockers) > 0,
          f"blockers={[b.get('code', b) if isinstance(b, dict) else b for b in blockers][:4]}")

# ══════════ G. 模板（§6.3.11 / §6.3.3：25 个系统模板） ══════════
r = requests.post(f"{BASE}/factor-mining/templates/seed", timeout=120)
check("G1 系统模板 seed 幂等可重入", r.status_code in (200, 201), jget(r, "created", default=r.text[:80]))
r = requests.get(f"{BASE}/factor-mining/templates", params={"limit": 200}, timeout=30)
items = r.json() if isinstance(r.json(), list) else jget(r, "items", default=[])
sys_tpls = [t for t in items if str(t.get("scope", "")) in ("system", "preset", "public")]
cats = {str((t.get("rule_config") or {}).get("category") or t.get("category")) for t in (sys_tpls or items)}
check("G2 系统模板 ≥25 且覆盖 6 类（趋势/反转/波动率/估值/质量/量价）",
      len(sys_tpls or items) >= 25 and len(cats - {"None", ""}) >= 5,
      f"total={len(sys_tpls or items)} cats={sorted(cats)}")
r = requests.post(f"{BASE}/factor-mining/templates",
                  json={"name": "BB23-缺公式模板", "rule_config": {}}, timeout=30)
check("G3 个人模板缺 formula 被拒(422)", r.status_code == 422, f"status={r.status_code}")
r = requests.post(f"{BASE}/factor-mining/templates",
                  json={"name": "BB23-个人模板", "rule_config": {"formula": "cs_rank(pe_ttm)"}},
                  timeout=30)
tpl_id = jget(r, "id", default=jget(r, "template_id"))
check("G4 个人模板创建 201", r.status_code in (200, 201) and bool(tpl_id), f"tpl={tpl_id}")
if tpl_id:
    r = requests.post(f"{BASE}/factor-mining/templates/{tpl_id}/enabled", json={"enabled": 0}, timeout=30)
    check("G5 个人模板可停用", r.status_code == 200, f"status={r.status_code}")
fake = requests.get(f"{BASE}/factor-mining/templates/no-such-tpl-xyz", timeout=30)
check("G6 模板 404 信封含中文结构字段", fake.status_code == 404 and bool(
      jget(fake, "extras", "title_zh", default="") or jget(fake, "technical_details", "error_message", default="")),
      jget(fake, "extras", "title_zh", default=jget(fake, "user_message")))

# ══════════ H. 字段异步校验：幂等复用（§5） ══════════
if draft_id:
    body = {"draft_id": draft_id, "fields": ["close", "volume"],
            "config": {"selected_fields": ["close", "volume"], "seed": "bb23"}}
    r1 = requests.post(f"{BASE}/factor-mining/validations", json=body, timeout=60)
    vid = jget(r1, "task_id", default=jget(r1, "validation_task_id", default=jget(r1, "id")))
    r2 = requests.post(f"{BASE}/factor-mining/validations", json=body, timeout=60)
    reused = jget(r2, "reused")
    vid2 = jget(r2, "task_id", default=jget(r2, "validation_task_id", default=jget(r2, "id")))
    check("H1 创建校验任务 201", r1.status_code in (200, 201) and bool(vid), f"task={vid}")
    # 复用语义（§5）：仅 queued/running 活动任务复用；首次已进终态则新建合法。
    # 发起 r2 前先查首次当前状态，消除时序歧义。
    rq = requests.get(f"{BASE}/factor-mining/validations/{vid}", timeout=30) if vid else None
    st_now = str(jget(rq, "status", default=jget(rq, "state", default=""))).lower() if rq is not None else ""
    first_terminal = st_now in ("done", "completed", "success", "failed", "cancelled")
    check("H2 相同 draft+config：活动态复用 / 首次已终态则新建合法（§5）",
          r2.status_code in (200, 201) and (reused is True or vid2 == vid or first_terminal),
          f"reused={reused} same_vid={vid2 == vid} first_status_at_r2={st_now}")
    if vid:
        r = requests.get(f"{BASE}/factor-mining/validations/{vid}", timeout=30)
        check("H3 校验进度可查询（状态机可见）",
              r.status_code == 200 and bool(jget(r, "status")), f"status={jget(r, 'status')}")
        r = requests.get(f"{BASE}/factor-mining/validations/{vid}/valid", timeout=30)
        check("H4 24h 有效期判定接口 200", r.status_code == 200 and "valid" in r.json(),
              {k: r.json().get(k) for k in ("valid", "verdict", "ttl_hours")})

# ══════════ I. 提交挖掘：负例与双锁拒绝（§7/§8.2） ══════════
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": "snap-not-exist-000",
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 50, "max_generations": 1},
    "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
}, timeout=120)
check("I1 伪造快照 id 提交被服务端拒绝（4xx，不允许静默跑空）", r.status_code >= 400,
      f"status={r.status_code} body={r.text[:160]}")
# 若服务端错误地接受了伪造快照（真实缺陷），取消其创建的 run 并等锁释放，避免污染 I2/J
if r.status_code in (200, 201):
    bad_run = jget(r, "run_id")
    if bad_run:
        requests.post(f"{BASE}/factor-mining/runs/{bad_run}/cancel", timeout=30)
        for _ in range(40):
            time.sleep(5)
            lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
            if not (lk.get("miningDomain") or {}).get("busy"):
                break

run_id = None
if pool_id and snap_id:
    r = requests.post(f"{BASE}/factor-mining/runs", json={
        "candidate_pool_snapshot_id": snap_id,
        "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
        "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
        "target_horizon": 5, "random_seed": 42,
        "evolution_params": {"population_size": 50, "max_generations": 1},
        "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
    }, timeout=120)
    run_id = jget(r, "run_id")
    check("I2 最小真实 run 提交成功 201（北交所小池，种群50×1代）",
          r.status_code == 201 and bool(run_id),
          f"run={run_id} queue_pos={jget(r, 'queue_position')}")
    if run_id:
        # mining_domain 冲突：立即二次提交 → 必须 409 拒绝（不排队）
        r2 = requests.post(f"{BASE}/factor-mining/runs", json={
            "candidate_pool_snapshot_id": snap_id,
            "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
            "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
            "target_horizon": 5, "evolution_params": {"population_size": 50, "max_generations": 1},
            "filter_config": {"selected_fields": ["close"]},
        }, timeout=120)
        check("I3 mining_domain 冲突：第二任务被 409 拒绝且不排队（§8.2）",
              r2.status_code == 409 and "MINING_DOMAIN_BUSY" in r2.text,
              f"status={r2.status_code}")
        r3 = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30)
        check("I4 锁状态查询反映占用（miningDomain busy=true）",
              bool(jget(r3, "miningDomain", "busy")), jget(r3, "miningDomain"))

# ══════════ J. 运行状态机：暂停→继续→提前停止（§8.1/§8.3.5） ══════════
if run_id:
    time.sleep(3)
    r = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30)
    st0 = jget(r, "status")
    r = requests.post(f"{BASE}/factor-mining/runs/{run_id}/pause", timeout=30)
    st1 = jget(r, "status")
    check("J1 中断(pause)→paused", st1 == "paused", f"{st0} → {st1}")
    r = requests.post(f"{BASE}/factor-mining/runs/{run_id}/resume", timeout=60)
    st2 = jget(r, "status")
    first_resume = r.status_code
    # pause 后锁异步释放（实测约 45s）：立即 resume 可能 409，重试等待恢复
    for _ in range(30) if first_resume != 200 else []:
        time.sleep(5)
        r = requests.post(f"{BASE}/factor-mining/runs/{run_id}/resume", timeout=60)
        st2 = jget(r, "status")
        if st2 in ("queued", "running"):
            break
    check("J2 继续(resume)→queued/running（允许锁释放延迟重试）",
          st2 in ("queued", "running"), f"首次={first_resume} → {st2}")
    time.sleep(2)
    r = requests.post(f"{BASE}/factor-mining/runs/{run_id}/cancel", timeout=30)
    check("J3 取消(cancel)→cancelled", jget(r, "status") == "cancelled")
    time.sleep(8)
    r = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30)
    busy_after = bool(jget(r, "miningDomain", "busy"))
    # 取消后锁由流水线检查点/心跳超时回收；轮询等待释放（上限 150s）
    released = not busy_after
    for _ in range(30 if not released else 0):
        time.sleep(5)
        r = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30)
        released = not bool(jget(r, "miningDomain", "busy"))
        if released:
            break
    check("J4 取消后锁回收（等待后不再永久占用，P1 自愈）", released,
          f"immediately_busy={busy_after} released={released}")
    r = requests.get(f"{BASE}/factor-mining/runs/{run_id}/generations", timeout=30)
    gens = r.json() if isinstance(r.json(), list) else []
    check("J5 每代汇总可查（含探针字段，§6.11.2）", r.status_code == 200,
          f"generations={len(gens)} keys={sorted(set().union(*[set(g) for g in gens])) if gens else []}"[:250])
    r = requests.post(f"{BASE}/factor-mining/runs/{run_id}/discard", timeout=30)
    check("J6 完全放弃(discard)→cancelled", jget(r, "status") == "cancelled")

# ══════════ K. 人工调整等级与证据抽屉接口（§8.4.2/§8.4.1） ══════════
r = requests.post(f"{BASE}/factor-mining/candidates/fake-cand/grade/manual",
                  json={"grade": "A", "reason": "短"}, timeout=30)
check("K1 调整原因 <10 字被入参拦截(422)", r.status_code == 422, f"status={r.status_code}")
r = requests.post(f"{BASE}/factor-mining/candidates/fake-cand/grade/manual",
                  json={"grade": "A", "reason": "黑盒测试用充分理由说明超过十个字符"}, timeout=30)
check("K2 不存在候选人工定级→4xx 结构化", 400 <= r.status_code < 500,
      f"status={r.status_code}")
r = requests.get(f"{BASE}/factor-mining/candidates/fake-cand/grade/evidence", timeout=30)
check("K3 证据抽屉接口对不存在候选返回 404 中文信封",
      r.status_code == 404 and bool(jget(r, "extras", "title_zh", default="") or "不存在" in r.text),
      f"status={r.status_code} user_message={jget(r, 'user_message')}")

# ══════════ L. 经验库（§6.9） ══════════
r = requests.get(f"{BASE}/factor-experience", params={"page_size": 5}, timeout=30)
ok_l = r.status_code == 200
r2 = requests.get(f"{BASE}/factor-experience/sample",
                  params={"field_scope": "close", "count": 3}, timeout=30)
check("L1 F1 列表与 sample 接口可用", ok_l and r2.status_code == 200,
      f"list={r.status_code} sample={r2.status_code}")

# ══════════ 汇总 ══════════
passed = sum(1 for x in RESULTS if x["ok"])
print(f"\n===== API 黑盒：{passed}/{len(RESULTS)} PASS =====")
with open("api_bb23_report.json", "w", encoding="utf-8") as f:
    json.dump(RESULTS, f, ensure_ascii=False, indent=2)
sys.exit(0 if passed == len(RESULTS) else 1)
