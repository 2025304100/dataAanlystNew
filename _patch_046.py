# 扩展 046：为 decision_evidence 补齐 wps_024 中定义的 8 个命名索引（ORM index=True
# 生成的索引名与 wps_024 create_index(命名) 不同，导致其 downgrade 的 8 条
# drop_index 报错 no such index）。
from pathlib import Path
p = Path(r"D:/ai_project/dataAanlystNew/alembic/versions/2026_09_02_0048_wps_0023_046_fix_idempotency_status_index_rollback.py")
text = p.read_text(encoding="utf-8")

old_doc = '''"""修正迁移：补齐 idempotency_records 在 ORM WP0-2 改 schema 后缺失的列/索引，
避免旧 wps_0023_024 迁移 rollback 时抛出 "no such index: ix_idempotency_records_*"。'''
new_doc = '''"""修正迁移：补齐 idempotency_records / decision_evidence 在
Base.metadata.create_all (ORM WP0-2 schema) 先跑后缺失、或命名不一致的列与命名索引，
避免旧 wps_0023_024 迁移 downgrade 抛出 no such index / no such column。'''

text = text.replace(old_doc, new_doc)

# 替换 indexes 列表块（在 idempotency_records 三索引后追加 decision_evidence 八索引）
old_idx = '''    indexes = [
        ("ix_idempotency_records_resource_type", ["resource_type"]),
        ("ix_idempotency_records_status",        ["status"]),
        ("ix_idempotency_records_created_at",    ["created_at"]),
    ]
    for idx_name, cols in indexes:
        if _index_exists("idempotency_records", idx_name):
            continue
        try:
            op.create_index(idx_name, "idempotency_records", cols, if_not_exists=True)
        except TypeError:
            # 旧版 Alembic / 方言不支持 if_not_exists，fallback 手工检查
            if not _index_exists("idempotency_records", idx_name):
                op.create_index(idx_name, "idempotency_records", cols)'''

new_idx = '''    # (A) idempotency_records：wps_024 原始命名 3 索引
    idem_indexes = [
        ("ix_idempotency_records_resource_type", ["resource_type"]),
        ("ix_idempotency_records_status",        ["status"]),
        ("ix_idempotency_records_created_at",    ["created_at"]),
    ]
    for idx_name, cols in idem_indexes:
        _safe_create_index(idx_name, "idempotency_records", cols)

    # (B) decision_evidence：wps_024 原始 8 条命名索引。
    #     注意 ORM DecisionEvidence 对应列都有 index=True，但 SQLAlchemy 默认名
    #     不是 ix_decision_evidence_*，导致 wps_024 downgrade drop_index 炸。
    #     8 条全部"列存在才建"，保证与 env.py 幂等补丁一致。
    evidence_indexes = [
        ("ix_decision_evidence_decision_run_id",       ["decision_run_id"]),
        ("ix_decision_evidence_strategy_snapshot_id",  ["strategy_snapshot_id"]),
        ("ix_decision_evidence_portfolio_id",          ["portfolio_id"]),
        ("ix_decision_evidence_symbol_id",             ["symbol_id"]),
        ("ix_decision_evidence_trade_date",            ["trade_date"]),
        ("ix_decision_evidence_action",                ["action"]),
        ("ix_decision_evidence_action_subtype",        ["action_subtype"]),
        ("ix_decision_evidence_rejection_reason",      ["rejection_reason"]),
        ("ix_decision_evidence_idempotency_key",       ["idempotency_key"]),
        ("ix_decision_evidence_created_at",            ["created_at"]),
    ]
    for idx_name, cols in evidence_indexes:
        # 任一列缺失就跳过（env.py _create_index 已做同等守卫）
        insp = sa.inspect(op.get_bind())
        cols_exist = True
        if insp.has_table("decision_evidence"):
            existing_cols = {c["name"] for c in insp.get_columns("decision_evidence")}
            if any(str(c) not in existing_cols for c in cols):
                cols_exist = False
        else:
            cols_exist = False
        if cols_exist:
            _safe_create_index(idx_name, "decision_evidence", cols)'''

# 在替换前先把 _safe_create_index 辅助函数挂到 upgrade 的前面（或复用 try/except 逻辑）
helper = '''
def _safe_create_index(idx_name: str, table_name: str, columns) -> None:
    """幂等安全建索引：列缺失/索引已存在都静默跳过。

    首选 if_not_exists=True（alembic 原生），遇到 TypeError（旧版不支持关键字）
    或索引名不存在时再走 _index_exists 手工检查 + 原生 create_index。
    """
    if _index_exists(table_name, idx_name):
        return
    try:
        op.create_index(idx_name, table_name, columns, if_not_exists=True)
    except TypeError:
        if not _index_exists(table_name, idx_name):
            op.create_index(idx_name, table_name, columns)
'''

# 插入 helper 函数到 _index_exists 定义后面
insert_after = '''def _index_exists(table_name: str, index_name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table_name):
        return False
    return any(i.get("name") == index_name for i in insp.get_indexes(table_name))
'''

if insert_after in text and helper.strip() not in text:
    text = text.replace(insert_after, insert_after + helper.lstrip("\n"), 1)

if old_idx in text:
    text = text.replace(old_idx, new_idx)
else:
    raise SystemExit("OLD INDEX BLOCK NOT FOUND")

p.write_text(text, encoding="utf-8")
print("EXTEND-046-DECISION-EVIDENCE OK")
