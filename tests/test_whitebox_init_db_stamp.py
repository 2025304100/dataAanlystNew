"""钉住"create_all 与 alembic 互认"这件事。

背景（实测发现的结构性缺陷）：新机器没有 config/db_config.json 时会退回
`settings.database_url`（系统 TEMP 下的 SQLite），建库完全靠启动时的 init_db()
（Base.metadata.create_all + _ensure_* 补丁）。但 init_db **从不写 alembic_version**，
于是 alembic 认为这库"从未迁移过"，下次 `upgrade head` 会从第 1 个迁移重放 ——
撞"表已存在"或撞迁移与模型的类型不匹配（实测 0032 在 MySQL 上就是 1215）。

这里验证修复后的行为：表缺失才补、已存在绝不改、补完 alembic 认账。
"""
from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import app.models  # noqa: F401  —— 下面按包扫描确保 metadata 完整
from app.db.base import Base
from app.db.init_db import _alembic_head_revision, _stamp_alembic_head_if_absent

pytestmark = pytest.mark.whitebox

for _m in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{_m.name}")


@pytest.fixture()
def fresh_engine(tmp_path):
    """一个"新机器刚启动完"的库：只有 create_all 的结果，没有 alembic_version。"""
    db = tmp_path / "fresh.sqlite3"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    yield engine, db
    engine.dispose()


def _versions(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if ("alembic_version",) not in rows:
            return []
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]


def test_fresh_db_has_no_alembic_version(fresh_engine):
    """前提检查：create_all 确实不写版本表（这就是缺陷的来源）。"""
    engine, _ = fresh_engine
    with engine.connect() as conn:
        existing = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )}
    assert "alembic_version" not in existing, (
        "create_all 本不该建 alembic_version；若哪天它出现了，本文件的其它断言要重审"
    )
    assert Base.metadata.tables, "模型没加载出来，测试无意义"


def test_stamp_creates_head_when_table_absent(fresh_engine):
    engine, db = fresh_engine
    head = _alembic_head_revision()
    assert head, "取不到唯一 head（迁移分叉未合并？）——这本身就该修"

    _stamp_alembic_head_if_absent(engine)

    assert _versions(db) == [head], "新库应被标记为当前 head，使 alembic 认账"


def test_stamp_is_idempotent(fresh_engine):
    engine, db = fresh_engine
    head = _alembic_head_revision()
    _stamp_alembic_head_if_absent(engine)
    _stamp_alembic_head_if_absent(engine)  # 第二次不得报错、不得插重复行
    assert _versions(db) == [head]


def test_stamp_never_overwrites_existing_version(fresh_engine):
    """已有库的版本记录是它的演进事实，绝不能被启动流程改掉。"""
    engine, db = fresh_engine
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(128) NOT NULL, "
            "PRIMARY KEY (version_num))"
        )
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('some_old_revision')")

    _stamp_alembic_head_if_absent(engine)

    assert _versions(db) == ["some_old_revision"], "已有版本记录必须保持不变"


def test_upgraded_chain_after_stamp_is_noop(fresh_engine, tmp_path):
    """最强的一条：stamp 之后 `alembic upgrade head` 应认这个库、什么都不做且成功。

    用子进程跑，避免 env.py 的 fileConfig 影响本测试进程的 logger（项目旧坑）。
    """
    import os
    import subprocess
    import sys

    engine, db = fresh_engine
    head = _alembic_head_revision()
    _stamp_alembic_head_if_absent(engine)
    engine.dispose()

    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["ALEMBIC_DATABASE_URL"] = f"sqlite:///{db}"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, f"upgrade head 失败：\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    assert _versions(db) == [head], "stamp 后 alembic 不应重放历史迁移"
