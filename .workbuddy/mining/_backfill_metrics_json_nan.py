#!/usr/bin/env python3
"""TD3 DoD ②③：metrics_json 存量 NaN/Infinity 字面量清洗（backfill）。

三张表（与 5 个写入点一一对应；data_quality_snapshots 属数据治理域，
TD3 not_do 明确排除，跨域待 owner）：
  factor_evaluation_runs     <- factor_evaluator.finalize_evaluation_run /
                                wp5_eval_task 压力指标合并
  factor_shadow_observations <- factor_shadow.record_shadow_observation
  factor_model_runs          <- ridge_model 训练落库 / factor_models 离线最小训练

判定（**不用正则改写**，pitfall 钦定）：`json.loads(raw, parse_constant=reject)` ——
Python json 的 parse_constant 钩子**只**在 NaN / Infinity / -Infinity 三个非标准
字面量出现时被调用，抛异常即「该行含非法字面量」；JSONDecodeError（坏行）单独
计数、不修改，避免误伤。

修复：`parse_constant=lambda s: None` 重新解析（三个字面量精确变 None）→
`json.dumps(clean_json_tree(fixed), allow_nan=False)` 写回 —— clean_json_tree
兜底 1e999 这类「合法字面量溢出为 inf」的值（与写入侧同口径），其余内容原样。

安全与幂等：
- --dry-run 只读报告，不写库（DoD②：报告存量含字面量的行数）；
- --apply 只改「判定命中」的行；**复扫必须为 0**，非 0 视为失败非零退出（DoD③）；
- 二跑判定命中 0 行 = 幂等，可重跑。

用法：
  .venv/Scripts/python.exe .workbuddy/mining/_backfill_metrics_json_nan.py --dry-run
  .venv/Scripts/python.exe .workbuddy/mining/_backfill_metrics_json_nan.py --apply
退出码：0 = 通过；1 = 失败（原因见输出）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import and_, create_engine, or_, select  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402
from app.core.db_numeric import clean_json_tree  # noqa: E402
import app.models  # noqa: F401, E402  （触发 ORM 注册）
from app.db.base import Base  # noqa: E402

TABLES = (
    "factor_evaluation_runs",
    "factor_shadow_observations",
    "factor_model_runs",
)


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON literal: {name}")


def _clean_row(raw: str) -> str:
    """把含 NaN/Infinity/-Infinity 字面量的 JSON 文本洗成合法 JSON。"""
    fixed = json.loads(raw, parse_constant=lambda s: None)  # 三个字面量 → None
    return json.dumps(clean_json_tree(fixed), ensure_ascii=False, allow_nan=False)


def scan(engine, table: str) -> tuple[list[tuple[object, str]], int]:
    """返回 (命中行 [(id, raw)], 非法 JSON 行数)。只读。"""
    tbl = Base.metadata.tables[table]
    with engine.connect() as conn:
        rows = conn.execute(
            select(tbl.c.id, tbl.c.metrics_json).where(
                and_(
                    tbl.c.metrics_json.isnot(None),
                    # LIKE 只是预过滤（减少拉取）；判定一律走严格 loads
                    or_(
                        tbl.c.metrics_json.like("%NaN%"),
                        tbl.c.metrics_json.like("%Infinity%"),
                    ),
                )
            )
        ).all()
    hits: list[tuple[object, str]] = []
    invalid = 0
    for rid, raw in rows:
        try:
            json.loads(raw, parse_constant=_reject_constant)
        except json.JSONDecodeError:
            invalid += 1          # 坏行不修（避免误伤），仅报告
        except ValueError:
            hits.append((rid, raw))  # parse_constant 抛出 = 含非标准字面量
    return hits, invalid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只读报告，不写库")
    g.add_argument("--apply", action="store_true", help="清洗命中行并复扫验证")
    args = parser.parse_args()

    engine = create_engine(build_mysql_url(load_db_config()))
    total_hits = 0
    total_cleaned = 0
    total_invalid = 0
    fail = False

    try:
        for table in TABLES:
            hits, invalid = scan(engine, table)
            total_hits += len(hits)
            total_invalid += invalid
            sample = [str(r[0]) for r in hits[:10]]
            print(f"[{table}] nan_literal_rows={len(hits)} invalid_json={invalid} "
                  f"sample_ids={sample}{'...' if len(hits) > 10 else ''}")

            if args.apply and hits:
                with engine.begin() as conn:
                    tbl = Base.metadata.tables[table]
                    for rid, raw in hits:
                        conn.execute(
                            tbl.update()
                            .where(tbl.c.id == rid)
                            .values(metrics_json=_clean_row(raw))
                        )
                # 复扫必须为 0（幂等验证）
                hits2, invalid2 = scan(engine, table)
                total_cleaned += len(hits) - len(hits2)
                print(f"[{table}] rescan: nan_literal_rows={len(hits2)} "
                      f"invalid_json={invalid2}")
                if hits2:
                    fail = True
                    print(f"❌ [{table}] 复扫仍有 {len(hits2)} 行含字面量 —— 清洗未生效")
    finally:
        engine.dispose()

    print("-" * 60)
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'} "
          f"total_nan_literal_rows={total_hits} total_invalid_json={total_invalid} "
          f"cleaned={total_cleaned}")

    if args.dry_run:
        print(f"DoD② {'达标' if not fail else '未达标'}：报告完成，未写库")
        return 1 if fail else 0
    if fail:
        return 1
    print("DoD③ 达标：--apply 完成，复扫为 0 行（幂等，可重跑）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
