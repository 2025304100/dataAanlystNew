"""T07 正向验证：外键**真的在生效**吗？（事务内测试，最后 ROLLBACK，不留数据）

为什么不能只看 information_schema：FK 存在 ≠ FK 被强制。
MyISAM 的历史教训正是「定义在、强制无」。故做行为验证：
  1. 插入 members 行，pool_id 指向**不存在**的池 → 必须被拒（MySQL 1452）
  2. 建池 + 成员，然后**硬删**该池 → 成员必须被级联删除（验证 ondelete=CASCADE）
  3. 孤儿 run_id → generations 必须被拒
  4. 全程在事务内，结束 ROLLBACK

⚠️ 用 ORM 表对象插入（`Base.metadata.tables[...].insert()`）而不是裸 SQL：
   模型用的是 **Python 端默认值**（`default=_utcnow` / `default=0`），
   对应列在库里**没有** server_default，裸 SQL 会报
   `Field 'included_at' doesn't have a default value`（本脚本第一版就踩了这个）。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(".").resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.models  # noqa: F401,E402
from app.db.base import Base  # noqa: E402
from app.core.config import build_mysql_url, load_db_config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

CAT = Base.metadata.tables
eng = create_engine(build_mysql_url(load_db_config()), pool_pre_ping=True)
results: list[tuple[str, bool, str]] = []

POOL_ID = "__t07_pool__"
GEN_ID = "__t07_gen__"

conn = eng.connect()
trans = conn.begin()
try:
    # ── 用例 1：孤儿 pool_id 必须被拒 ──
    print("=== 用例 1：孤儿 pool_id 必须被拒绝 ===")
    blocked, detail = False, ""
    try:
        conn.execute(CAT["training_candidate_pool_members"].insert().values(
            pool_id="__t07_nonexistent_pool__", symbol_id=1))
    except IntegrityError as exc:
        blocked = True
        detail = str(getattr(exc, "orig", exc))[:110]
    results.append(("孤儿 pool_id 被拒", blocked, detail))
    print(f"  {'OK' if blocked else 'FAIL'}  被拒={blocked}  {detail}")

    # ── 用例 2：删除池 → 级联删除成员 ──
    print()
    print("=== 用例 2：删除池必须级联删除成员 ===")
    conn.execute(CAT["training_candidate_pools"].insert().values(
        id=POOL_ID, name="T07 级联测试", source_type="filter", status="draft"))
    conn.execute(CAT["training_candidate_pool_members"].insert().values(
        pool_id=POOL_ID, symbol_id=999001))
    cnt = lambda: conn.execute(text(
        "SELECT COUNT(*) FROM training_candidate_pool_members WHERE pool_id=:p"),
        {"p": POOL_ID}).scalar()
    before = cnt()
    conn.execute(text("DELETE FROM training_candidate_pools WHERE id=:p"), {"p": POOL_ID})
    after = cnt()
    cascaded = int(before or 0) == 1 and int(after or 0) == 0
    results.append(("删除池级联删除成员", cascaded, f"删前={before} 删后={after}"))
    print(f"  {'OK' if cascaded else 'FAIL'}  成员数 删前={before} 删后={after}")

    # ── 用例 3：孤儿 run_id 必须被拒 ──
    # ⚠️ `factor_mining_generations.id` 是**自增整型**（不是 str(64) 那种），
    #    所以这里不传 id，让它自增；否则会先撞 1366 类型错误而不是外键错误。
    print()
    print("=== 用例 3：孤儿 run_id 必须被拒绝 ===")
    blocked3, detail3 = False, ""
    try:
        conn.execute(CAT["factor_mining_generations"].insert().values(
            run_id="__t07_nonexistent_run__", generation=0))
    except IntegrityError as exc:
        blocked3 = True
        detail3 = str(getattr(exc, "orig", exc))[:110]
    results.append(("孤儿 run_id 被拒", blocked3, detail3))
    print(f"  {'OK' if blocked3 else 'FAIL'}  被拒={blocked3}  {detail3}")
finally:
    trans.rollback()
    left = conn.execute(text(
        "SELECT (SELECT COUNT(*) FROM training_candidate_pools WHERE id=:p) AS p, "
        "(SELECT COUNT(*) FROM training_candidate_pool_members WHERE pool_id=:p) AS m, "
        "(SELECT COUNT(*) FROM factor_mining_generations WHERE run_id='__t07_nonexistent_run__') AS g"
    ), {"p": POOL_ID}).fetchone()
    conn.close()
eng.dispose()

print()
print(f"  事务已 ROLLBACK，残留 = pool:{left[0]} member:{left[1]} generation:{left[2]}（应均为 0）")
print()
print("=== 汇总 ===")
for name, passed, _ in results:
    print(f"  {'OK  ' if passed else 'FAIL'} {name}")
ok = all(r[1] for r in results) and tuple(left) == (0, 0, 0)
print()
print("  结论:", "✅ 外键被真实强制（含 CASCADE），且测试数据已回滚" if ok
      else "❌ 外键未生效或数据残留")
raise SystemExit(0 if ok else 1)
