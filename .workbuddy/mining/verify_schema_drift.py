#!/usr/bin/env python3
"""迁移 ↔ ORM 漂移检查（可复用的通用工具）。v2（TD5 扩展）

为什么需要它
------------
`app/db/init_db.py` 的 `_auto_align_all_schema()` 会在启动时**静默补齐**缺失的表/列/索引。
好处是应用不会崩，**坏处是它会掩盖迁移文件本身的错误** —— 你永远不知道
到底是迁移写对了，还是 auto-align 事后补上了。

本脚本在**不启动应用、不跑 auto-align** 的前提下，直接比对
「数据库实际结构」与「ORM 声明结构」，把漂移暴露出来。

检查项（每张表，v2 共 7 类覆盖）：
  - 表是否存在（库缺表 / ORM 缺模型）
  - 列集合是否一致（区分「库多列」/「ORM 多列」两个方向）
  - 索引是否一致（缺索引 / 多索引，含唯一索引）
  - 命名唯一约束是否落实为唯一索引
  - 外键是否一致（缺外键 / 多外键 / ondelete 动作不符）
  - **表引擎**（v2/TD5）：ORM 显式声明了 `mysql_engine` 则精确比对；
    未声明时兜底检查实际引擎必须为 InnoDB（MyISAM/MEMORY 无事务、
    静默吞 FK —— T01/T07 的真实事故形态）。仅对 MySQL 生效。
  - **字符集/排序规则**（v2/TD5）：ORM 显式声明了 `mysql_charset` /
    `mysql_collate` 则精确比对；未声明时只做 **warning 级**检查
    （表字符集 ≠ 库默认字符集才提示，不计入漂移、不触发 exit 1，
    避免库默认本身不是 utf8mb4 时把 127 张表全部误报）。
    每张表的实际引擎/字符集都会进 `--json` 报告供人工核对。
  - **检查约束 CHECK**（v2/TD5）：按「约束名」+「归一化 SQL 文本」双轨比对，
    名字对不上但文本一致的视为已实现（规避 MySQL 自动命名差异）。

真实案例
--------
**真实案例 1（T01，2026-09-16）**：0053 首版漏了 `mysql_engine="InnoDB"` →
14 张表建成 MyISAM；并用短索引名导致同一索引建两遍（20 个冗余索引）；还漏了 3 个列。
全部被 auto-align 静默补齐，是靠本脚本才发现的。

**真实案例 2（T07，2026-09-16）**：MyISAM **静默忽略外键定义** →
引擎后来被转成 InnoDB，但**外键永久丢失**（真实库 14 张表 FK=0，而同库其它表有 95 个外键）。
当年本工具**没有外键检查**，所以报「0/0/0 无漂移」却仍缺 6 个外键。
→ 已补齐检查；修复见修订 `0054_wps_0023_052_mining_fk_constraints`。

**防假绿铁律（TD5 pitfall）**：每加一类覆盖，必须用**已知缺陷样本**验证「能报出」——
漂移检查漏掉哪一类，那一类缺陷就隐形。`--selftest` 用内存 sqlite 建缺陷表回灌，
同时用一张完全正常的表验证零误报。

用法：
    # 检查全部 ORM 表（最慢，最全）
    .venv/Scripts/python.exe .workbuddy/mining/verify_schema_drift.py

    # 只检查指定表
    ... verify_schema_drift.py --tables factor_mining_runs,task_locks

    # 检查某个 alembic 修订引入的表（从迁移文件的 _ALL_TABLES 读）
    ... verify_schema_drift.py --from-revision 2026_09_16_0053_wps_0023_051_factor_mining_core.py

    # 指定数据库（默认走 db_config.json → MySQL；可覆盖为 scratch sqlite）
    ALEMBIC_DATABASE_URL=sqlite:///x.db ... verify_schema_drift.py --url-from-env

    # 结构化 JSON 报告（evidence 用；报告成功产出即 exit 0，漂移详情在 JSON 里）
    ... verify_schema_drift.py --json > schema_drift_audit.json

    # 防假绿自检（内存 sqlite 缺陷样本回灌，不连任何真实库）
    ... verify_schema_drift.py --selftest

退出码：
    默认模式       0 = 无漂移；1 = 存在漂移
    --json 模式    0 = 报告成功产出（是否漂移看 JSON.summary；连接失败等硬错误仍非 0）
    --selftest     0 = 自检通过；2 = 自检失败（某类覆盖报不出已知缺陷 / 正常样本误报）
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import CheckConstraint  # noqa: E402

# SQLite 为 PK / UNIQUE 自动生成的内部索引名，不是漂移
_SQLITE_AUTO = re.compile(r"^sqlite_autoindex_")

# 每张表可能出现的全部问题键（用于 has_drift 判定与 JSON by_class 统计）
_PROBLEM_KEYS = (
    "missing_orm", "missing_db",
    "col_db_only", "col_orm_only",
    "missing_idx", "extra_idx", "missing_uq",
    "missing_fk", "extra_fk", "fk_mismatch",
    "engine", "charset", "missing_check",
)


def resolve_db_url(use_env_only: bool) -> str:
    if use_env_only:
        raw = os.getenv("ALEMBIC_DATABASE_URL")
        if not raw:
            sys.exit("[FATAL] --url-from-env 需要设置 ALEMBIC_DATABASE_URL")
        return raw
    sys.path.insert(0, str(REPO / "alembic"))
    from app.core.config import build_mysql_url, load_db_config, settings  # noqa: PLC0415

    cfg = load_db_config()
    if bool(cfg.get("use_mysql")) and isinstance(cfg.get("mysql"), dict):
        return build_mysql_url(cfg)
    return settings.database_url


def tables_from_revision(fname: str) -> list[str]:
    """从迁移文件的 `_ALL_TABLES` 抽取表名（正则，不 import 迁移）。"""
    path = REPO / "alembic" / "versions" / fname
    if not path.exists():
        sys.exit(f"[FATAL] 找不到迁移文件：{path}")
    text = path.read_text(encoding="utf-8")
    m = re.search(r"_ALL_TABLES[^=]*=\s*\(([^)]*)\)", text, re.S)
    if not m:
        sys.exit(f"[FATAL] {fname} 里没有 _ALL_TABLES 元组，请用 --tables 显式指定")
    return re.findall(r'"([^"]+)"', m.group(1))


def _norm_sqltext(raw) -> str:
    """CHECK 约束文本归一：小写、去空白/反引号/引号/方括号、去尾部分号。

    ORM 里写的 ``status IN ('a', 'b')`` 与 MySQL DDL 存的
    ```status` IN ('a','b')`` 归一后应相同，才能跨方言比对。
    """
    # 注意：raw 可能是 SQLAlchemy TextClause，其 __bool__ 会抛 TypeError ——
    # 必须先 str() 再判空，不能用 `raw or ""` 短路。
    s = "" if raw is None else str(raw)
    s = s.lower()
    s = re.sub(r"[\s`'\"\[\]]+", "", s)
    return s.rstrip(";")


def _check_table_options(orm_kwargs: dict, db_engine, db_collation, db_default_charset):
    """引擎/字符集/排序规则比对（纯函数，便于 --selftest 单测）。

    返回 ``(hard, warns)``：
      hard = {"engine": [...], "charset": [...]}   —— 计入漂移
      warns = [...]                                —— 仅提示，不计入漂移
    """
    hard = {"engine": [], "charset": []}
    warns: list[str] = []
    if not db_engine:  # sqlite 等方言没有表选项概念
        return hard, warns

    # ── 引擎 ──
    orm_engine = str(orm_kwargs.get("mysql_engine") or "").upper()
    actual_engine = str(db_engine or "").upper()
    if orm_engine:
        if actual_engine != orm_engine:
            hard["engine"].append(f"ORM={orm_engine} 实际={actual_engine}")
    elif actual_engine != "INNODB":
        hard["engine"].append(
            f"实际={actual_engine}（非 InnoDB：无事务、静默吞 FK —— T01/T07 事故形态）"
        )

    # ── 字符集 / 排序规则 ──
    db_collation = str(db_collation or "")
    db_charset = db_collation.split("_")[0]
    orm_charset = str(orm_kwargs.get("mysql_charset") or "").lower()
    orm_collate = str(orm_kwargs.get("mysql_collate") or "").lower()
    if orm_charset and db_charset != orm_charset:
        hard["charset"].append(f"ORM={orm_charset} 实际={db_charset}({db_collation})")
    if orm_collate and db_collation.lower() != orm_collate:
        hard["charset"].append(f"排序规则 ORM={orm_collate} 实际={db_collation}")
    if not orm_charset and not orm_collate and db_default_charset:
        # 未声明 → 只做 warning 级：表字符集与库默认不一致（utf8mb3 残留等）
        if db_charset and db_charset != str(db_default_charset).lower():
            warns.append(f"表字符集 {db_charset}({db_collation}) ≠ 库默认 {db_default_charset}")
    return hard, warns


def _missing_checks(orm_checks: list[tuple], db_checks: list[dict]) -> list[str]:
    """CHECK 约束比对：名字精确 → 文本归一兜底（规避 MySQL 自动命名差异）。"""
    db_named = {
        str(c.get("name")): _norm_sqltext(c.get("sqltext"))
        for c in db_checks
        if c.get("name") is not None or c.get("sqltext")
    }
    db_texts = set(db_named.values())
    out: list[str] = []
    for name, ntext in orm_checks:
        if name and name in db_named:
            if db_named[name] == ntext:
                continue
            out.append(f"{name} 文本不符: 期望[{ntext}] 实际[{db_named[name]}]")
        elif ntext and ntext in db_texts:
            continue  # 名字对不上（自动命名）但文本一致 → 视为已实现
        else:
            out.append(name or (ntext[:60] if ntext else "<空>"))
    return out


def check_table(tb, insp, *, is_mysql: bool = False,
                db_default_charset=None, table_options=None) -> dict:
    """对单张表做全量比对，返回结构化结果（--selftest 与 main 共用）。"""
    t = tb.name
    res: dict = {"table": t, "check_error": None, "warnings": [], "options": None}
    for k in _PROBLEM_KEYS:
        res[k] = []

    # ── 列（区分方向）──
    db_cols = {c["name"] for c in insp.get_columns(t)}
    orm_cols = set(tb.columns.keys())
    res["col_db_only"] = sorted(db_cols - orm_cols)
    res["col_orm_only"] = sorted(orm_cols - db_cols)

    # ── 索引 / 唯一约束 ──
    orm_idx = {ix.name for ix in tb.indexes}
    db_idx = {i["name"] for i in insp.get_indexes(t)}
    db_idx_norm = {n for n in db_idx if not _SQLITE_AUTO.match(n)}
    db_unique_cols = {
        tuple(i["column_names"]) for i in insp.get_indexes(t) if i.get("unique")
    }
    # 命名 UNIQUE 约束必须单独查 get_unique_constraints()：
    # SQLite 把它实现为 sqlite_autoindex_*，MySQL 则会同时出现在两个来源中。
    try:
        db_uq_names = {
            c["name"] for c in insp.get_unique_constraints(t) if c.get("name")
        }
        db_uq_cols = {
            tuple(c["column_names"])
            for c in insp.get_unique_constraints(t)
            if c.get("column_names")
        }
    except NotImplementedError:
        db_uq_names, db_uq_cols = set(), set()

    # 少索引：ORM 声明了索引，但库里的索引名和「唯一索引/唯一约束的列组合」都没覆盖
    missing_idx = []
    for n in sorted(orm_idx):
        if n in db_idx_norm:
            continue
        cols = tuple(c.name for ix in tb.indexes if ix.name == n for c in ix.columns)
        if cols in db_unique_cols or cols in db_uq_cols:
            continue  # 已被作为唯一约束实现
        missing_idx.append(n)

    # 少唯一约束：ORM 声明了命名 UNIQUE，库里既无同名约束、也无同列组合的唯一索引/约束
    orm_uq_cols = {}
    for con in tb.constraints:
        if con.__class__.__name__ == "UniqueConstraint" and con.name:
            orm_uq_cols[con.name] = tuple(c.name for c in con.columns)
    missing_uq = [
        name for name, cols in sorted(orm_uq_cols.items())
        if name not in db_uq_names and cols not in db_uq_cols and cols not in db_unique_cols
    ]

    extra_idx = sorted(db_idx_norm - orm_idx - set(orm_uq_cols))
    res["missing_idx"] = missing_idx
    res["extra_idx"] = extra_idx
    res["missing_uq"] = missing_uq

    # ── 外键比对 ──
    # 键用「本地列元组」而不是约束名：MySQL 的 FK 名是自动生成的
    # （<table>_ibfk_<n>），按名字比会误报。SQLite 把 FK 放在 PRAGMA foreign_key_list。
    orm_fk: dict[tuple[str, ...], tuple[str, tuple[str, ...], str]] = {}
    for con in tb.constraints:
        if con.__class__.__name__ != "ForeignKeyConstraint":
            continue
        elements = list(con.elements)
        if not elements:
            continue
        local = tuple(e.parent.name for e in elements)
        orm_fk[local] = (
            elements[0].column.table.name,
            tuple(e.column.name for e in elements),
            (elements[0].ondelete or "").upper(),
        )

    db_fk: dict[tuple[str, ...], tuple[str, tuple[str, ...], str]] = {}
    try:
        for fk in insp.get_foreign_keys(t):
            cols = tuple(fk.get("constrained_columns") or [])
            if not cols:
                continue
            ref_table = (fk.get("referred_table") or "").split(".")[-1]
            opts = fk.get("options") or {}
            db_fk[cols] = (
                ref_table,
                tuple(fk.get("referred_columns") or []),
                str(opts.get("ondelete") or "").upper(),
            )
    except Exception:  # noqa: BLE001 - 各方言 get_foreign_keys 支持度不一，取不到就当无 FK
        db_fk = {}

    res["missing_fk"] = sorted(orm_fk.keys() - db_fk.keys())
    res["extra_fk"] = sorted(db_fk.keys() - orm_fk.keys())
    fk_mismatch: list[str] = []
    for cols in sorted(orm_fk.keys() & db_fk.keys()):
        o_ref_t, o_ref_c, o_del = orm_fk[cols]
        d_ref_t, d_ref_c, d_del = db_fk[cols]
        if (o_ref_t, o_ref_c, o_del) != (d_ref_t, d_ref_c, d_del):
            fk_mismatch.append(
                f"{cols} 期望->{o_ref_t}{o_ref_c}({o_del or 'NO ACTION'}) "
                f"实际->{d_ref_t}{d_ref_c}({d_del or 'NO ACTION'})"
            )
    res["fk_mismatch"] = fk_mismatch

    # ── 引擎 / 字符集（仅 MySQL；v2/TD5 新增）──
    if is_mysql and table_options is not None:
        opts = table_options.get(str(t).lower())
        if opts:
            hard, warns = _check_table_options(
                tb.dialect_kwargs, opts[0], opts[1], db_default_charset
            )
            res["engine"] = hard["engine"]
            res["charset"] = hard["charset"]
            res["warnings"] = warns
            res["options"] = {
                "engine": str(opts[0] or ""),
                "collation": str(opts[1] or ""),
                "charset": str(opts[1] or "").split("_")[0],
            }
        # opts 缺失 = information_schema 无此行（has_table 已确保存在，理论不可达）

    # ── 检查约束（v2/TD5 新增；MySQL 8.0.16+/SQLite 支持）──
    try:
        db_checks = list(insp.get_check_constraints(t))
    except Exception as exc:  # noqa: BLE001 - 方言不支持时如实记录，不静默
        res["check_error"] = f"{type(exc).__name__}: {exc}"
        db_checks = []
    if db_checks or not res["check_error"]:
        orm_checks = [
            (con.name, _norm_sqltext(con.sqltext))
            for con in tb.constraints
            if isinstance(con, CheckConstraint)
        ]
        res["missing_check"] = _missing_checks(orm_checks, db_checks)

    res["stats"] = {"idx": len(db_idx_norm), "cols": len(db_cols), "fk": len(db_fk)}
    return res


def selftest() -> int:
    """防假绿自检：内存 sqlite 缺陷样本回灌 + 表选项纯函数用例。不连任何真实库。"""
    from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String
    from sqlalchemy import Table, UniqueConstraint, create_engine, inspect
    from sqlalchemy import text as sql_text

    failures: list[str] = []

    # ── A. 表选项（引擎/字符集）纯函数用例（MySQL 特性，sqlite 演练不了）──
    # (用例名, orm_kwargs, 实际engine, 实际collation, 库默认charset,
    #  期望: engine命中, charset命中, warning命中)
    option_cases = [
        ("MyISAM 无 ORM 声明", {}, "MyISAM", "utf8mb4_general_ci", "utf8mb4",
         True, False, False),
        ("InnoDB 无 ORM 声明", {}, "InnoDB", "utf8mb4_general_ci", "utf8mb4",
         False, False, False),
        ("ORM 要 InnoDB 实际 MyISAM", {"mysql_engine": "InnoDB"}, "MyISAM",
         "utf8mb4_general_ci", "utf8mb4", True, False, False),
        ("ORM 字符集 utf8mb4 实际 utf8", {"mysql_charset": "utf8mb4"}, "InnoDB",
         "utf8_general_ci", "utf8", False, True, False),
        ("排序规则精确比对", {"mysql_collate": "utf8mb4_0900_ai_ci"}, "InnoDB",
         "utf8mb4_general_ci", "utf8mb4", False, True, False),
        ("字符集与库默认不符(仅warning)", {}, "InnoDB", "utf8_general_ci", "utf8mb4",
         False, False, True),
        ("库默认与表一致(零输出)", {}, "InnoDB", "utf8mb4_general_ci", "utf8mb4",
         False, False, False),
    ]
    for (label, kwargs, deng, dcoll, ddflt, e_eng, e_chs, e_warn) in option_cases:
        hard, warns = _check_table_options(kwargs, deng, dcoll, ddflt)
        if bool(hard["engine"]) != e_eng:
            failures.append(f"[表选项] {label}: engine 命中={bool(hard['engine'])} 期望={e_eng}")
        if bool(hard["charset"]) != e_chs:
            failures.append(f"[表选项] {label}: charset 命中={bool(hard['charset'])} 期望={e_chs}")
        if bool(warns) != e_warn:
            failures.append(f"[表选项] {label}: warning 命中={bool(warns)} 期望={e_warn}")

    # ── B. 结构检查：内存 sqlite + 手工 DDL 建已知缺陷表 ──
    md = MetaData()
    Table("st_parent", md, Column("id", Integer, primary_key=True))
    Table("st_good", md,
          Column("id", Integer, primary_key=True),
          Column("name", String(50), nullable=False),
          Column("code", String(32), nullable=False),
          Column("portfolio_id", Integer, ForeignKey("st_parent.id")),
          Index("ix_st_good_name", "name"),
          UniqueConstraint("code", "portfolio_id", name="uq_st_good_dedupe"),
          CheckConstraint("id >= 0", name="ck_st_good_id"))
    Table("st_missing_fk", md,
          Column("id", Integer, primary_key=True),
          Column("portfolio_id", Integer, ForeignKey("st_parent.id")))
    Table("st_missing_uq", md,
          Column("id", Integer, primary_key=True),
          Column("a", Integer), Column("b", Integer),
          UniqueConstraint("a", "b", name="uq_st_missing_uq"))
    Table("st_missing_check", md,
          Column("id", Integer, primary_key=True),
          Column("v", Integer),
          CheckConstraint("v > 0", name="ck_st_missing_check"))
    Table("st_extra_idx", md,
          Column("id", Integer, primary_key=True),
          Column("p", Integer),
          Index("ix_st_extra_idx_p", "p"))
    Table("st_col_defect", md,
          Column("id", Integer, primary_key=True),
          Column("expected_col", Integer))
    Table("st_missing_idx", md,
          Column("id", Integer, primary_key=True),
          Column("q", Integer),
          Index("ix_st_missing_idx_q", "q"))
    Table("st_fk_mismatch", md,
          Column("id", Integer, primary_key=True),
          Column("parent_id", Integer,
                 ForeignKey("st_parent.id", ondelete="CASCADE")))

    eng = create_engine("sqlite://")
    ddl = [
        "CREATE TABLE st_parent (id INTEGER PRIMARY KEY)",
        """CREATE TABLE st_good (
               id INTEGER PRIMARY KEY,
               name VARCHAR(50) NOT NULL,
               code VARCHAR(32) NOT NULL,
               portfolio_id INTEGER,
               CHECK (id >= 0),
               FOREIGN KEY (portfolio_id) REFERENCES st_parent (id))""",
        "CREATE INDEX ix_st_good_name ON st_good (name)",
        "CREATE UNIQUE INDEX uq_st_good_dedupe ON st_good (code, portfolio_id)",
        # 缺 FK：ORM 声明了 REFERENCES，DDL 里没有 —— 模拟 MyISAM 吞 FK 后转 InnoDB
        "CREATE TABLE st_missing_fk (id INTEGER PRIMARY KEY, portfolio_id INTEGER)",
        # 缺唯一约束
        "CREATE TABLE st_missing_uq (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER)",
        # 缺 CHECK
        "CREATE TABLE st_missing_check (id INTEGER PRIMARY KEY, v INTEGER)",
        "CREATE TABLE st_extra_idx (id INTEGER PRIMARY KEY, p INTEGER)",
        "CREATE INDEX ix_st_extra_idx_p ON st_extra_idx (p)",
        "CREATE INDEX ix_st_extra_idx_extra ON st_extra_idx (id, p)",
        # 列双向缺失：DB 有 legacy_col，ORM 有 expected_col
        "CREATE TABLE st_col_defect (id INTEGER PRIMARY KEY, legacy_col INTEGER)",
        # 缺普通索引
        "CREATE TABLE st_missing_idx (id INTEGER PRIMARY KEY, q INTEGER)",
        # FK ondelete 不符：ORM=CASCADE，DDL 默认 NO ACTION
        """CREATE TABLE st_fk_mismatch (
               id INTEGER PRIMARY KEY,
               parent_id INTEGER,
               FOREIGN KEY (parent_id) REFERENCES st_parent (id))""",
    ]
    with eng.begin() as conn:
        for stmt in ddl:
            conn.execute(sql_text(stmt))
    insp = inspect(eng)

    def run(name: str) -> dict:
        return check_table(md.tables[name], insp, is_mysql=False)

    def expect(name: str, cond: bool, label: str) -> None:
        if not cond:
            failures.append(f"[结构] {name}: {label}")

    res = run("st_good")
    bad = [k for k in _PROBLEM_KEYS if res[k]]
    expect("st_good", not bad and not res["warnings"],
           f"正常样本零误报被破坏: {bad + res['warnings']}")

    res = run("st_missing_fk")
    expect("st_missing_fk", "portfolio_id" in [fk[0] for fk in res["missing_fk"]],
           f"缺外键未报出: {res['missing_fk']}")

    res = run("st_missing_uq")
    expect("st_missing_uq", "uq_st_missing_uq" in res["missing_uq"],
           f"缺唯一约束未报出: {res['missing_uq']}")

    res = run("st_missing_check")
    expect("st_missing_check", bool(res["missing_check"]),
           f"缺检查约束未报出: {res['missing_check']}")

    res = run("st_extra_idx")
    expect("st_extra_idx", "ix_st_extra_idx_extra" in res["extra_idx"],
           f"多索引未报出: {res['extra_idx']}")

    res = run("st_col_defect")
    expect("st_col_defect",
           "legacy_col" in res["col_db_only"] and "expected_col" in res["col_orm_only"],
           f"列差异(双向)未报出: db_only={res['col_db_only']} orm_only={res['col_orm_only']}")

    res = run("st_missing_idx")
    expect("st_missing_idx", "ix_st_missing_idx_q" in res["missing_idx"],
           f"缺索引未报出: {res['missing_idx']}")

    res = run("st_fk_mismatch")
    expect("st_fk_mismatch", bool(res["fk_mismatch"])
           and "CASCADE" in res["fk_mismatch"][0],
           f"FK ondelete 不符未报出: {res['fk_mismatch']}")

    n = len(option_cases) + 8
    if failures:
        print("❌ selftest 失败（防假绿破防）:")
        for f in failures:
            print("  -", f)
        return 2
    print(f"✅ selftest 通过：{n} 个用例 —— 7 类覆盖全部「能报出」，正常样本零误报")
    print("   覆盖: 缺FK / 缺唯一约束 / 缺CHECK / 多索引 / 缺索引 / 列双向差异 / "
          "FK ondelete 不符 / 引擎(MyISAM) / 字符集 / 排序规则")
    return 0


def _fetch_table_options(eng):
    """MySQL：一次性抓全库表选项 + 库默认字符集。返回 (options, default_charset)。"""
    from sqlalchemy import text as sql_text

    with eng.connect() as conn:
        rows = conn.execute(sql_text(
            "SELECT TABLE_NAME AS tn, ENGINE AS eng, TABLE_COLLATION AS coll "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        )).mappings().all()
        options = {str(r["tn"]).lower(): (r["eng"], r["coll"]) for r in rows}
        dflt = conn.execute(sql_text("SELECT @@character_set_database")).scalar()
    return options, dflt


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="verify_schema_drift.py",
        description="迁移 ↔ ORM 漂移检查（v2：7 类覆盖 + --json + --selftest）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--tables", help="逗号分隔的表名；默认检查全部 ORM 表")
    ap.add_argument("--from-revision", metavar="FILE", help="从迁移文件的 _ALL_TABLES 取表名")
    ap.add_argument("--url-from-env", action="store_true",
                    help="只用 ALEMBIC_DATABASE_URL（用于 scratch 库）")
    ap.add_argument("--quiet-ok", action="store_true", help="只打印有问题的表")
    ap.add_argument("--json", action="store_true",
                    help="stdout 只输出结构化 JSON（成功产出即 exit 0）")
    ap.add_argument("--selftest", action="store_true",
                    help="防假绿自检：内存 sqlite 缺陷样本回灌，不连真实库")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    from sqlalchemy import create_engine, inspect  # noqa: PLC0415
    import app.models  # noqa: F401, PLC0415  （触发 ORM 注册）
    from app.db.base import Base  # noqa: PLC0415

    url = resolve_db_url(args.url_from_env)
    shown = re.sub(r"://[^@]*@", "://***@", url)

    if args.tables:
        targets = [t.strip() for t in args.tables.split(",") if t.strip()]
    elif args.from_revision:
        targets = tables_from_revision(args.from_revision)
    else:
        targets = sorted(Base.metadata.tables)

    eng = create_engine(url, pool_pre_ping=True)
    insp = inspect(eng)
    is_mysql = eng.dialect.name == "mysql"

    table_options = None
    db_default_charset = None
    if is_mysql:
        table_options, db_default_charset = _fetch_table_options(eng)

    drift_rows: list[dict] = []
    warn_rows: list[dict] = []
    problems: list[str] = []
    ok_count = 0

    if not args.json:
        print(f"DB  : {shown}")
        print(f"范围: {len(targets)} 张表")

    for t in targets:
        tb = Base.metadata.tables.get(t)
        if tb is None:
            problems.append(f"{t}: ORM 里没有这张表（迁移建了但无模型？）")
            drift_rows.append({"table": t, "missing_orm": ["表在迁移/库里存在但无 ORM 模型"]})
            if not args.json:
                print(f"  [MISSING-ORM] {t}")
            continue
        if not insp.has_table(t):
            problems.append(f"{t}: 数据库里不存在")
            drift_rows.append({"table": t, "missing_db": ["ORM 声明了但库里没有这张表"]})
            if not args.json:
                print(f"  [MISSING-DB ] {t}")
            continue

        res = check_table(tb, insp, is_mysql=is_mysql,
                          db_default_charset=db_default_charset,
                          table_options=table_options)
        has_drift = any(res[k] for k in _PROBLEM_KEYS)

        if has_drift:
            problems.append(t)
            drift_rows.append(res)
            if not args.json:
                print(f"  [DRIFT] {t}")
                if res["col_db_only"] or res["col_orm_only"]:
                    print(f"          列差异   : 库多={res['col_db_only']} ORM多={res['col_orm_only']}")
                if res["missing_idx"]:
                    print(f"          缺索引   : {res['missing_idx']}")
                if res["extra_idx"]:
                    print(f"          多索引   : {res['extra_idx']}")
                if res["missing_uq"]:
                    print(f"          缺唯一约束: {res['missing_uq']}")
                if res["missing_fk"]:
                    print(f"          缺外键   : {res['missing_fk']}   <-- 迁移可能被 MyISAM 吞掉了 FK 定义")
                if res["extra_fk"]:
                    print(f"          多外键   : {res['extra_fk']}")
                for item in res["fk_mismatch"]:
                    print(f"          外键不符 : {item}")
                if res["engine"]:
                    print(f"          引擎不符 : {res['engine']}")
                if res["charset"]:
                    print(f"          字符集   : {res['charset']}")
                if res["missing_check"]:
                    print(f"          缺检查约束: {res['missing_check']}")
                if res["check_error"]:
                    print(f"          ⚠️ 检查约束读取失败: {res['check_error']}")
        elif res["warnings"] or res["check_error"]:
            warn_rows.append({
                "table": t,
                "warnings": res["warnings"] + (
                    [f"检查约束读取失败: {res['check_error']}"] if res["check_error"] else []
                ),
                "options": dict(zip(("engine", "collation"),
                                    table_options.get(str(t).lower(), (None, None))))
                if is_mysql else {},
            })
            if not args.json:
                print(f"  [WARN ] {t}  {warn_rows[-1]['warnings']}")
        else:
            ok_count += 1
            if not args.json and not args.quiet_ok:
                st = res["stats"]
                extra = ""
                if is_mysql and table_options and table_options.get(str(t).lower()):
                    eng_, coll_ = table_options[str(t).lower()]
                    extra = f" 引擎{str(eng_ or '?').upper()}"
                print(f"  [OK]    {t}  索引{st['idx']:2d} 列{st['cols']:2d} "
                      f"外键{st['fk']:2d}{extra}")

    by_class = {k: sum(1 for r in drift_rows if r.get(k)) for k in _PROBLEM_KEYS}
    notes: list[str] = []
    if warn_rows:
        notes.append(f"{len(warn_rows)} 张表有 warning（字符集与库默认不一致 / CHECK 读取降级），未计入漂移")
    notes.append("字符集全量明细见 drift_tables/warn_rows 的 options.collation 字段")

    if args.json:
        full_options = {}
        if is_mysql and table_options:
            for t in targets:
                o = table_options.get(str(t).lower())
                if o:
                    full_options[t] = {
                        "engine": str(o[0] or ""),
                        "collation": str(o[1] or ""),
                        "charset": str(o[1] or "").split("_")[0],
                    }
        payload = {
            "tool": "verify_schema_drift.py",
            "version": "2.0 (TD5 扩展)",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "db": shown,
            "dialect": eng.dialect.name,
            "db_default_charset": db_default_charset,
            "checks_covered": [
                "column", "index", "unique_constraint", "foreign_key",
                "engine", "charset", "check_constraint",
            ],
            "summary": {
                "total": len(targets),
                "drift": len(drift_rows),
                "ok": ok_count,
                "warn": len(warn_rows),
                "by_class": by_class,
            },
            "notes": notes,
            "drift_tables": drift_rows,
            "warn_rows": warn_rows,
            "table_options": full_options,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
        eng.dispose()
        return 0  # 报告成功产出即 0（DoD 管道语义）；漂移详情看 JSON.summary

    eng.dispose()
    print()
    if problems:
        print(f"❌ 存在漂移：{len(problems)}/{len(targets)} 张表不一致")
        print("   注意：应用启动时 _auto_align_all_schema 会自动补齐这些差异，")
        print("         所以『应用能跑』并不代表迁移是对的。请修**迁移文件**，")
        print("         而不是依赖 auto-align 兜底。")
        return 1
    print(f"✅ 无漂移：{len(targets)} 张表与 ORM 完全一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
