#!/usr/bin/env python3
"""TD2 收工登记（幂等）：
1. PROGRESS.json：tasks["TD2"] → done（finished_at/artifacts/evidence）+ observation 上报
2. 自检 _selfcheck_conflict.py
重跑安全：TD2 已 done 时只刷新 evidence/artifacts，不重复追加 observation。
修复后两连跑（_td2_sweep1/2.txt）与跨域回归（_td2_regression.txt）汇总行动态解析；
除 backtest_apply 既有 2 失败外出现任何 failed/error、或两连跑非 27 passed 全绿，拒绝登记。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


def _summary_of(path: pathlib.Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return next((l.strip("= ") for l in reversed(lines) if re.search(r"\d+ (passed|failed|error)", l)), "?")


def _exit_of(path: pathlib.Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return next((l for l in reversed(lines) if l.startswith("EXIT=")), "EXIT=?")


# ---- 解析回归与扫除修复后两连跑 ----
reg_sum = _summary_of(HERE / "_td2_regression.txt")
reg_exit = _exit_of(HERE / "_td2_regression.txt")
m_fail = re.search(r"(\d+) failed", reg_sum)
m_err = re.search(r"(\d+) error", reg_sum)
if (int(m_err.group(1)) if m_err else 0) or (int(m_fail.group(1)) if m_fail else 0) > 2:
    sys.exit(f"[FATAL] 回归出现非预期失败，拒绝登记：{reg_sum}")
sweep_sums = []
for name in ("_td2_sweep1.txt", "_td2_sweep2.txt"):
    s = _summary_of(HERE / name)
    e = _exit_of(HERE / name)
    if "27 passed" not in s or "failed" in s or "error" in s or e != "EXIT=0":
        sys.exit(f"[FATAL] {name} 非 27 passed/exit 0，拒绝登记：{s}（{e}）")
    sweep_sums.append((s, e))

prog_path = HERE / "PROGRESS.json"
prog = json.loads(prog_path.read_text(encoding="utf-8"))
td2 = prog.setdefault("tasks", {}).setdefault("TD2", {})

td2.update({
    "status": "done",
    "agent": "agent-senior-dev",
    "finished_at": now,
    "artifacts": [
        "tests/integration/conftest.py（六处：① setup 侧持 _cp_lock 重置控制面缓存并锁外 dispose，"
        "与 DatabaseManager 改绑原子完成；② teardown 侧 tmp unlink 前同款重置；"
        "③ Section 0 containment 防线——wp5_eval_task/trade_calendar/factor_stress/factor_registry "
        "共 11 个模块绑定 pristine 快照+每用例身份比对还原；④ manager 引擎 dispose（unlink 前按锁捕获）；"
        "⑤ unlink 失败诊断日志（仅失败时落盘 .workbuddy/mining/_td2_unlink_failures.txt）；"
        "⑥ 终修=进程级孤儿扫除：gc.get_objects 找出所有未关闭的 SQLAlchemy Session/Connection "
        "一律 close + unlink 重试×4——engine.dispose 只关池内连接、关不掉 checked-out 泄漏 session）",
        ".workbuddy/mining/_td2_cleanup_litter.py、_td2_recyclebin_probe.py"
        "（遗留垃圾清理脚本与回收站去向探针，均幂等带审计）",
        "证据文件：_td2_repro_b_alone/pair/p1/p2/fixed.txt（最小复现四连）、"
        "_td2_dod_run1/2.txt（DoD 首轮两连跑）、_td2_sweep1/2.txt（残留终修后两连跑）、"
        "_td2_unlink_failures.txt（38 条 PermissionError 定位日志）、_td2_regression.txt（跨域回归）、"
        "_td2_e2e_solo.txt、_td2_sanity.txt",
    ],
    "evidence": [
        "最小复现：test_blocker_worker_exception→test_blocker_stress_failure 固定顺序连跑="
        "1 failed（58.7s，PendingRollbackError @ async_tasks.py:716 _expire_stale_tasks），"
        "各自单跑绿→顺序依赖实锤；修复后同对 6.87s 全绿",
        "根因①（顺序污染主根因）：app/db/session.py get_control_session_local 模块级缓存 "
        "_cp_engine/_cp_factory（_cp_lock 双检、_make_control_plane_factory 一次性读 dm.engine.url、"
        "无失效接口）→首用例 tmp SQLite URL 被冻结，后续用例控制面轮询已 unlink 死库；"
        "修复=conftest setup/teardown 测试侧重置缓存+释放句柄（按 not_do 未改 app/）",
        "根因②（e2e 残余）：blockers SyntheticMockBase 用 patch.object(wp5_eval_task, 'FactorWarehouse')"
        " 打模块级名字绑定（MagicMock 按 horizon=5 查表返回 batch_t5_syn，h=6→None），"
        "泄漏时 worker 在 wp5_eval_task.py:1633 解析模块绑定拿到 MagicMock→任务 3 报 "
        "eval.data.target_horizon_unavailable；实锤链：e2e 任务1 latest_batch_id=batch_t5_syn"
        "（该串只存在于 blockers 测试文件；真实数仓 factor_targets 只有 target_5d_return）；"
        "e2e 单跑 1 passed。修复=containment 防线使其无害化",
        "DoD① pytest tests/integration -q → 27 passed / 51.96s / exit 0（开工基线 17 passed+10 failed；"
        "e2e 在 blockers 之后跑正是泄漏场景，containment 防线同跑验证）",
        "DoD② 连跑第二遍 → 27 passed / 36.00s / exit 0（顺序无关、可重复）",
        "残留深挖（收尾复核发现，历史 318 个遗留的同源机理）：DoD 首轮两跑全绿但各遗 ~19 个 .sqlite3"
        "（38 个，PermissionError(13) 被 except 吞掉）；诊断日志 38 条、19 个用例两跑完全一致且全为"
        "启动 TestClient 的用例——lifespan 启动路径（initialize_runtime_database 二次建引擎/"
        "_auto_start_universe_init 等）在进程内留下 checked-out 的泄漏 session，"
        "而 engine.dispose() 只关池内连接关不掉 checked-out，teardown 只 dispose fixture 引擎",
        f"终修后全新两连跑（⑥落地）：{sweep_sums[0][0]}（{sweep_sums[0][1]}）、"
        f"{sweep_sums[1][0]}（{sweep_sums[1][1]}）；两跑 unlink 失败日志均 0 条、"
        "仓库根 integration_* 残留 0——unlink 静默失败根治",
        f"跨域回归（TD4 同款 5 文件+test_whitebox_numeric_columns，136 项）：{reg_sum}（{reg_exit}）；"
        "2 失败=backtest_apply 的 test_portfolio_not_found_raises / test_missing_symbol_skipped"
        "（TD4 已做 Float 基线对照实证的既有失败），1 xfailed=auto_trade 预期项；"
        "TD2 只改 tests/integration/conftest.py，与跨域测试无交集",
        "遗留垃圾：仓库根 integration_* 共 318 个（110 sqlite3 377.5MB+208 wal/shm 58.4MB≈436MB）"
        "经用户批复清零（228 个经 shim 回收站路径、90 个 raw unlink）",
    ],
})

obs = prog.setdefault("observations", [])
obs_note = (
    "TD2 完成：tests/integration 整目录 27/27 两连跑 exit 0（开工基线 17 passed+10 failed），"
    "终修后 unlink 失败 0 条、根残留 0——历史 318 个遗留的 unlink 静默失败机理已根治。"
    "【上报裁决】根治建议给 app/db/session.py 增加控制面缓存失效/重建接口——本卡写权限仅 "
    "tests/integration/，本次用测试侧重置兜底；生产多引擎切换场景会复现同病。"
    "【app 侧泄漏挂观察（非本卡范围）】TestClient 用例（19/27）存在进程内 checked-out 泄漏 "
    "session——SQLAlchemy dispose 关不掉，测试侧用 gc 孤儿扫除兜底；泄漏源在 app lifespan 启动路径"
    "（二次建引擎/universe 自动初始化等），建议后续卡用同样诊断日志法定位到具体模块。"
    "【次根因挂观察】blockers 对 wp5_eval_task.FactorWarehouse 模块绑定的 patch 泄漏，"
    "具体泄漏用例未定位（containment 防线已无害化）。"
    "【环境发现待用户决定】D 盘回收站积 8,714 个 integration_* 条目共 1,055MB——历史所有会话的 "
    "pytest teardown unlink 经 SAFE_DELETE shim 全进回收站，超出本次批复的仓库根 436MB 范围未动；"
    "可选：整桶清空回收站，或按原始路径定向清理本项目条目。"
)
if isinstance(obs, list) and not any(obs_note[:40] in o for o in obs):
    obs.append(obs_note)
    print("[PROGRESS] observation 已追加")

prog["updated_at"] = now
prog["updated_by"] = "agent-senior-dev"
prog_path.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"[PROGRESS] TD2 → done（{now}）")

r = subprocess.run([sys.executable, str(HERE / "_selfcheck_conflict.py")],
                   capture_output=True, text=True, encoding="utf-8")
print("\n".join(r.stdout.strip().splitlines()[-3:]))
if r.returncode != 0:
    sys.exit(f"[FATAL] 自检失败\n{r.stdout}\n{r.stderr}")
