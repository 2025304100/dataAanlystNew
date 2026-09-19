#!/usr/bin/env python3
"""TD2 每日日志追加（幂等+回读校验）：
- 标记 "## TD2 完成：" 已存在则跳过（幂等）
- 扫除修复后两连跑/回归汇总行动态解析注入
- 写入后回读：标记唯一、尾部完整
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
LOG = HERE.parent / "memory" / "2026-09-18.md"
MARK = "## TD2 完成："


def _summary_of(path: pathlib.Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return next((l.strip("= ") for l in reversed(lines) if re.search(r"\d+ (passed|failed|error)", l)), "?")


sweep1 = _summary_of(HERE / "_td2_sweep1.txt")
sweep2 = _summary_of(HERE / "_td2_sweep2.txt")
reg = _summary_of(HERE / "_td2_regression.txt")

section = f"""{MARK}tests/integration 顺序污染治理（2026-09-18）

**开工核对**
- 基线实跑：tests/integration 整目录一次跑=17 passed+10 failed；失败全指向控制面轮询死库/隔离缺失。
- 主根因实锤：app/db/session.py get_control_session_local 模块级缓存（_cp_lock 双检）无失效接口，
  _make_control_plane_factory 一次性读 dm.engine.url——首用例 tmp URL 冻结，后续用例轮询死库。

**施工**
- 最小复现先行：test_blocker_worker_exception→test_blocker_stress_failure 固定顺序连跑红
  （58.7s PendingRollbackError @ async_tasks.py:716 _expire_stale_tasks）、各自单跑绿。
- conftest 六处：①setup 持 _cp_lock 重置控制面缓存+锁外 dispose（与 DatabaseManager 改绑原子）；
  ②teardown tmp unlink 前同款重置；③Section 0 containment 防线（wp5 相关 11 个模块绑定
  pristine 快照+每用例身份比对还原）；④manager 引擎 dispose；⑤unlink 失败诊断日志；
  ⑥终修=孤儿 session 扫除（见下）。
- e2e 残余实锤：blockers 泄漏 patch.object(wp5_eval_task, "FactorWarehouse") 模块绑定；
  e2e 任务1 latest_batch_id=batch_t5_syn 只存在于 blockers 文件（真实数仓无此串）。
- 残留深挖（收尾复核发现）：首轮 DoD 两跑全绿但各遗 ~19 个 .sqlite3（PermissionError(13) 被吞）；
  诊断日志 38 条、19 个用例两跑完全一致且全为 TestClient 用例——lifespan 启动路径在进程内留下
  checked-out 泄漏 session，engine.dispose() 只关池内连接关不掉 checked-out，dispose 管理引擎也无效；
  终修⑥：teardown 用 gc.get_objects 找出所有未关闭 SQLAlchemy Session/Connection 一律 close
  + unlink 重试×4——不管泄漏源在哪一律兜住。
- 环境处置（经用户批复）：仓库根 318 个遗留 integration_*（约436MB）清零；
  90 个 -shm 用 ENABLED=0 raw 真删；护栏 THRESHOLD=1000 放行 DoD 跑。

**DoD**
- ① 27 passed/51.96s/exit 0（基线 17+10）；② 连跑第二遍 27 passed/36.00s/exit 0。
- 终修后全新两连跑：{sweep1}、{sweep2}；unlink 失败 0 条、根残留 0——静默失败根治。
- 跨域回归 136 项（迁移链+组合/快照/回测/自动交易+数值列）：{reg}（仅 backtest_apply 2 既有失败）。

**教训**
- 全绿不等于干净：DoD 过了还要复核副产物（残留文件）——unlink 静默失败被 except 吞掉，
  诊断日志（用例名+异常）一条就锁定了 19/27 的泄漏模式；「19 个用例两跑完全一致」直接指向
  TestClient lifespan 路径。
- engine.dispose() 关不掉 checked-out 连接——「所有引擎都 dispose 过」不等于「没有句柄残留」，
  兜底要落在对象级（session/connection）而不是引擎级。
- 护栏 SystemExit 破坏 pytest teardown 状态机（下一用例 ERROR at setup）——二分实验被毒化两次才醒悟。
- Bash 无输出 120s 即 SIGTERM 且 stdout 缓冲全丢：清理脚本首跑 228 个文件删完静默死亡，靠 glob 复查定位。
- SAFE_DELETE shim 把一切 unlink 送回收站：D 盘已积 8,714 条目/1,055MB——「删除成功」不等于「磁盘回收」。
"""

if LOG.exists() and MARK in LOG.read_text(encoding="utf-8"):
    print("[log] TD2 段已存在，跳过（幂等）")
    sys.exit(0)

with LOG.open("a", encoding="utf-8") as f:
    f.write("\n" + section)

# 回读校验
back = LOG.read_text(encoding="utf-8")
cnt = back.count(MARK)
tail = back.rstrip().endswith("「磁盘回收」。")
if cnt != 1 or not tail:
    sys.exit(f"[FATAL] 回读校验失败：标记数={cnt} tail_ok={tail}")
print("[log] TD2 段已追加并回读校验通过")
print("[log] 时间戳:", _dt.datetime.now().isoformat(timespec="seconds"))
