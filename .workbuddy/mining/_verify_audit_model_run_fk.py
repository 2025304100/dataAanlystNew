#!/usr/bin/env python3
"""TD1 DoD ①：factor_audit_logs.model_run_id 外键**行为验证**（真实库）。

背景：information_schema 里有 FK 定义 ≠ 被强制执行（T07 教训），
本脚本做双行为验证，任何一条不过即非零退出：

  A. 孤儿插入被拒 —— 向 factor_audit_logs 插 model_run_id 指向不存在父行 →
     期望 MySQL **1452**（Cannot add or update a child row）。
  B. 删父行 SET NULL —— 先插测试父行（factor_model_runs）+ 引用它的子行，
     删除父行 → 子行 model_run_id 应被置 **NULL**（ondelete=SET NULL）。

安全与幂等：
- 全部测试行带唯一前缀 `td1_fk_test_`，脚本开头先清扫上次运行残留（可重跑），
  finally 里再清一次；
- 用 **ORM 表对象**（Base.metadata.tables[...].insert()）插入 —— 模型的
  Python 端默认值（default='{}' 等）才会生效（裸 SQL 会撞 NOT NULL）；
- 只增删自己的测试行，不碰任何业务数据。

用法：.venv/Scripts/python.exe .workbuddy/mining/_verify_audit_model_run_fk.py
退出码：0 = 双验证通过；1 = 失败（原因见输出）。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, inspect, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402
import app.models  # noqa: F401, E402  （触发 ORM 注册，否则 metadata 里没有表）
from app.db.base import Base  # noqa: E402

PREFIX = "td1_fk_test_"
AUDIT = Base.metadata.tables["factor_audit_logs"]
RUNS = Base.metadata.tables["factor_model_runs"]


def _fail(msg: str) -> int:
    print(f"❌ {msg}")
    return 1


def cleanup(engine, why: str) -> None:
    """删除本脚本名下所有测试行（子先父后，防约束拦路）。"""
    with engine.begin() as conn:
        c1 = conn.execute(
            AUDIT.delete().where(AUDIT.c.actor.like(PREFIX + "%"))
        ).rowcount
        c2 = conn.execute(
            RUNS.delete().where(RUNS.c.id.like(PREFIX + "%"))
        ).rowcount
    if c1 or c2:
        print(f"  [cleanup:{why}] 清理残留 子行x{c1} 父行x{c2}")


def main() -> int:
    url = build_mysql_url(load_db_config())
    eng = create_engine(url, pool_pre_ping=True)
    insp = inspect(eng)

    # ── 前置：FK 必须已存在（迁移未跑就先报清楚，不静默跳过）──────────
    fks = [
        fk for fk in insp.get_foreign_keys("factor_audit_logs")
        if "model_run_id" in (fk.get("constrained_columns") or [])
    ]
    if not fks:
        return _fail("factor_audit_logs.model_run_id 上没有外键 —— 请先执行 alembic upgrade head（0056）。")
    print(f"✅ 前置：FK 已存在（{fks[0].get('name')}，"
          f"-> {fks[0].get('referred_table')}.{fks[0].get('referred_columns')}，"
          f"ondelete={(fks[0].get('options') or {}).get('ondelete')}）")

    cleanup(eng, "开工清残留")

    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    run_id = f"{PREFIX}run_{int(time.time() * 1000)}"
    ok = True

    try:
        # ── A. 孤儿插入必须被拒（1452）────────────────────────────
        try:
            with eng.begin() as conn:
                conn.execute(AUDIT.insert().values(
                    action="train",
                    model_run_id=PREFIX + "no_such_parent",
                    actor=PREFIX + "orphan_probe",
                ))
            ok = _fail("A. 孤儿插入竟然成功了 —— FK 未被强制执行！")
        except IntegrityError as exc:
            orig = getattr(exc, "orig", None)
            code = getattr(orig, "args", [None])[0]
            if code == 1452:
                print("✅ A. 孤儿插入被拒：MySQL 1452（FK 强制执行）")
            else:
                ok = _fail(f"A. 被拒但错误码不是 1452（实际 {code}）：{orig}")
        except Exception as exc:  # noqa: BLE001
            ok = _fail(f"A. 出现意外异常：{exc!r}")

        # ── B. 删父行 → 子行 model_run_id 置 NULL ─────────────────
        if ok:
            with eng.begin() as conn:
                conn.execute(RUNS.insert().values(
                    id=run_id,
                    model_type="ridge",
                    status="training",
                    feature_versions_json="{}",
                    hyperparameters_json="{}",
                    metrics_json="{}",
                    created_at=now,
                ))
                r = conn.execute(AUDIT.insert().values(
                    action="train",
                    model_run_id=run_id,
                    actor=PREFIX + "setnull_probe",
                ))
                child_id = r.inserted_primary_key[0]

            got = eng.connect().execute(
                select(AUDIT.c.model_run_id).where(AUDIT.c.id == child_id)
            ).scalar()
            if got != run_id:
                ok = _fail(f"B. 子行落库时 model_run_id 应为父 id，实际 {got!r}")

            if ok:
                with eng.begin() as conn:
                    conn.execute(RUNS.delete().where(RUNS.c.id == run_id))
                got = eng.connect().execute(
                    select(AUDIT.c.model_run_id).where(AUDIT.c.id == child_id)
                ).scalar()
                if got is None:
                    print("✅ B. 删除父行后子行 model_run_id 已被置 NULL（SET NULL 生效）")
                else:
                    ok = _fail(f"B. 删父行后子行 model_run_id={got!r}，未置 NULL —— ondelete 未生效！")
    finally:
        cleanup(eng, "收尾清扫")
        eng.dispose()

    if ok:
        print("\n✅ TD1 行为验证通过：1452 拒孤儿 + SET NULL 级联，双验证成立。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
