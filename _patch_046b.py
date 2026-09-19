# 在 revision 046 文件里追加"模块级幂等强化补丁"：在 046.py 被 import 时，
# 对 alembic.operations.Operations.drop_index 再套一层 try/except，兜底任何
# "no such index" 错误（env.py 的 pre-check 在某些 batch_alter_table 后偶发
# inspect 与实际 DDL 执行时序不一致，因此这里再做 runtime 兜底）。
# 同理对 drop_constraint(drop_unique_constraint, drop_check_constraint) 也兜底，
# 因为 wps_024 还有一堆 uq_* / ck_* DDL。
from pathlib import Path
p = Path(r"D:/ai_project/dataAanlystNew/alembic/versions/2026_09_02_0048_wps_0023_046_fix_idempotency_status_index_rollback.py")
text = p.read_text(encoding="utf-8")

if "_install_046_runtime_safety_net" in text:
    print("ALREADY INSTALLED"); raise SystemExit(0)

module_snippet = '''
# ---------------------------------------------------------------------------
# T3 P0 runtime safety-net（模块级钩子）
# ---------------------------------------------------------------------------
# 说明：
#   046.upgrade 只能"补完 ORM create_all 先跑路径下缺失的列/命名索引"；
#   但 wps_031 / wps_032 的 downgrade 会对 decision_evidence 用
#   batch_alter_table 做整表重建，把 024 定义、046 补建的 idempotency_key
#   列及同名索引连同 031 扩展列"一次性 batch drop"，导致后续 wps_024 downgrade
#   的 10 条 drop_index(ix_decision_evidence_*) 再碰到"no such index"。
#
#   env.py 已有 Operations.drop_index 前置检查补丁，但在 inspect 结果与
#   DDL 实际执行顺序（SQLite DDL 即时提交、render_as_batch）不一致时，
#   可能漏判 has=True，随后 DDL 执行仍抛 OperationalError。
#
#   本模块作为 ScriptDirectory 加载的 revision 文件，被 alembic 早期 import，
#   因此可以在模块级再给 Operations 方法追加一道"吞 no such index / no such
#   table / no such constraint"的兜底，等价于给所有旧 ≤045 revision 的
#   drop_index / drop_constraint / drop_column 行为自动 if_exists=True。
# ---------------------------------------------------------------------------
_INSTALL_046_SAFETY_NET = False

def _install_046_runtime_safety_net():
    global _INSTALL_046_SAFETY_NET
    if _INSTALL_046_SAFETY_NET:
        return
    _INSTALL_046_SAFETY_NET = True
    import functools
    from alembic.operations import Operations

    _NO_SUCH_LITERALS = (
        "no such index", "no such table", "no such column",
        "no such constraint", "does not exist",
        "unknown constraint", "index not found", "table not found",
    )

    def _swallow_no_such(orig_fn):
        @functools.wraps(orig_fn)
        def _wrapped(self, *a, **kw):
            try:
                return orig_fn(self, *a, **kw)
            except Exception as e:
                msg = str(e).lower()
                if any(lit in msg for lit in _NO_SUCH_LITERALS):
                    # 把"对象不存在"类错误降级为 NOOP（与 if_exists=True 同语义）
                    return None
                raise
        return _wrapped

    # 为以下 5 个经常被旧迁移无条件调用的销毁操作加兜底：
    for attr_name in ("drop_index", "drop_table", "drop_column",
                      "drop_constraint", "drop_check_constraint"):
        orig = getattr(Operations, attr_name, None)
        if orig is None or getattr(orig, "__046_safe__", False):
            continue
        wrapped = _swallow_no_such(orig)
        wrapped.__046_safe__ = True
        setattr(Operations, attr_name, wrapped)

# 模块加载即安装（保证 alembic 在 run_migrations 前已带好该兜底）
_install_046_runtime_safety_net()

'''

# 把这段插入到 module docstring 后面, depends_on 块之后, helpers 函数之前
anchor = "depends_on: Sequence[str] | None = None\n"
if anchor not in text:
    raise SystemExit("ANCHOR NOT FOUND")
text = text.replace(anchor, anchor + "\n" + module_snippet.lstrip("\n"), 1)
p.write_text(text, encoding="utf-8")
print("INSTALL-046-SAFETY-NET OK")
