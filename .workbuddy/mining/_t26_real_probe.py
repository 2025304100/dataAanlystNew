# -*- coding: utf-8 -*-
"""T26 真实库写入试探（db_numeric P0：单测绿 ≠ 写入安全）。

走 store_experience 全链路写真实 MySQL gpfx：
  1. NaN metric → 库中 None（不可归 0）
  2. 四表同事务落库可回读
  3. 清理痕迹（按 experience_id 删净）
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa

from app.core.config import build_mysql_url, load_db_config
from app.models.factor_experience import (
    FactorExperience,
    FactorExperienceFieldDep,
    FactorExperienceMetric,
    FactorExperienceTag,
)
from app.services.factors.experience import service as svc

cfg = load_db_config()
url = build_mysql_url(cfg)
engine = sa.create_engine(url)
import sqlalchemy.orm as orm

Session = orm.sessionmaker(bind=engine)
db = Session()
failures = []
exp_id = None
try:
    ast = {
        "type": "Expression",
        "body": {
            "type": "Call", "func": "mean",
            "args": [
                {"type": "Name", "id": "close"},
                {"type": "Constant", "value": 20},
            ],
            "keywords": [],
        },
    }
    payload = {
        "formula_ast": ast,
        "source": "manual",
        "category": "trend",
        "metrics": [
            {"metric_type": "icir", "value": float("nan")},
            {"metric_type": "coverage", "value": 0.95},
        ],
        "task_context": {"market_env": "probe_env"},
        "field_layers": {"close": "A"},
    }
    exp_id, status = svc.store_experience(db, payload=payload)
    print(f"store -> ({exp_id[:16]}…, {status})")

    row = db.get(FactorExperience, exp_id)
    if row is None:
        failures.append("主表未回读")
    else:
        print(f"主表: template={row.formula_template!r} engine 检查见 drift 报告")
        if row.avg_icir is not None:
            failures.append(f"avg_icir 应为 None（NaN 输入），实得 {row.avg_icir!r}")

    metrics = db.execute(
        sa.select(FactorExperienceMetric).where(
            FactorExperienceMetric.experience_id == exp_id)).scalars().all()
    by_type = {m.metric_type: m.value for m in metrics}
    print(f"metrics: {by_type}")
    if by_type.get("icir") is not None:
        failures.append("NaN icir 未转 None")
    if by_type.get("coverage") is None:
        failures.append("coverage 有效值丢失")

    n_tags = db.execute(
        sa.select(sa.func.count()).select_from(FactorExperienceTag).where(
            FactorExperienceTag.experience_id == exp_id)).scalar_one()
    n_deps = db.execute(
        sa.select(sa.func.count()).select_from(FactorExperienceFieldDep).where(
            FactorExperienceFieldDep.experience_id == exp_id)).scalar_one()
    print(f"tags={n_tags} deps={n_deps}")
    if n_tags < 3 or n_deps != 1:
        failures.append(f"子表行数异常 tags={n_tags} deps={n_deps}")

    sample = svc.sample_experiences(
        db, market_env="probe_env", count=5)
    got = [i["experience_id"] for i in sample]
    print(f"sample(真实库) -> {len(got)} 条, 命中={exp_id[:16]}… in {got[0][:16]}…")
    if exp_id not in got:
        failures.append("真实库抽取未命中刚插入的经验")
finally:
    # 清理痕迹
    if exp_id:
        for model in (FactorExperienceMetric, FactorExperienceTag,
                      FactorExperienceFieldDep, FactorExperience):
            deleted = db.execute(
                sa.delete(model).where(model.experience_id == exp_id)
                if hasattr(model, "experience_id")
                else sa.delete(model).where(model.id == exp_id))
        db.commit()
        left = db.get(FactorExperience, exp_id)
        print("清理:", "完成" if left is None else "失败（残留）")
        if left is not None:
            failures.append("清理失败")
    db.close()
    engine.dispose()

print("=" * 60)
if failures:
    print("REAL-DB PROBE FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("REAL-DB PROBE OK —— 真实 MySQL 写入/回读/抽取/清理全通")
