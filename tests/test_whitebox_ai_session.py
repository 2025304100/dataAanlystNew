"""白盒测试 - WP-AI.1 AI 会话与审计数据模型。

覆盖 AISession / AIMessage / AIActionAudit 三张表的：
1. 表存在性（init_db 后表存在）
2. CRUD 服务（创建会话/添加消息/列表/归档/软删除/动作审计/确认/拒绝）
3. SQLite schema 补丁幂等性
4. MySQL 补丁函数存在性
5. 级联删除（删会话级联删消息；删消息级联删审计）
6. Token 累计（add_message 同步累加 session.total_tokens）
7. 字段完整性
"""
from __future__ import annotations

import inspect as _inspect
import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_ai_session_tables,
    _ensure_sqlite_ai_session_tables,
    init_db,
)
from app.models.ai_session import (
    AIActionAudit,
    AIMessage,
    AISession,
)
from app.services.ai_session_service import (
    ActionAuditNotFoundError,
    SessionNotFoundError,
    add_action_audit,
    add_message,
    archive_session,
    confirm_action,
    create_session,
    delete_session,
    get_session,
    list_sessions,
    reject_action,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_session(db_session, **kwargs) -> AISession:
    """创建一个最小可用的 AISession。"""
    defaults = {
        "title": "QA-Session",
        "source_page": "discovery",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    defaults.update(kwargs)
    return create_session(db_session, **defaults)


# ----------------------------------------------------------------------------
# 1. 表存在性
# ----------------------------------------------------------------------------


def test_ai_session_table_exists(db_session):
    """【WP-AI.1】init_db 后 ai_sessions / ai_messages / ai_action_audits 三张表存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "ai_sessions" in table_names
    assert "ai_messages" in table_names
    assert "ai_action_audits" in table_names


def test_init_db_creates_ai_session_tables(tmp_sqlite_url):
    """【WP-AI.1】init_db() 调用后三张表存在（端到端验证）。"""
    from app.db.manager import DatabaseManager

    mgr = DatabaseManager.get()
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")

    try:
        init_db()
        engine = mgr.engine
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "ai_sessions" in table_names
        assert "ai_messages" in table_names
        assert "ai_action_audits" in table_names
    finally:
        try:
            mgr.dispose()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 2. CRUD 服务：创建会话
# ----------------------------------------------------------------------------


def test_create_session(db_session):
    """【WP-AI.1】create_session 创建会话并读回字段。"""
    session = create_session(
        db_session,
        title="挖掘结果分析",
        source_page="discovery",
        provider="openai",
        model="gpt-4o-mini",
        profile_id="profile-001",
    )

    assert session.id is not None
    assert session.title == "挖掘结果分析"
    assert session.source_page == "discovery"
    assert session.provider == "openai"
    assert session.model == "gpt-4o-mini"
    assert session.profile_id == "profile-001"
    assert session.status == "active"
    assert session.total_tokens == 0
    assert session.total_cost is None
    assert session.deleted_at is None
    assert session.created_at is not None

    # 通过 get_session 读回
    fetched = get_session(db_session, session.id)
    assert fetched is not None
    assert fetched.id == session.id
    assert fetched.title == "挖掘结果分析"


# ----------------------------------------------------------------------------
# 3. CRUD 服务：添加消息
# ----------------------------------------------------------------------------


def test_add_message(db_session):
    """【WP-AI.1】add_message 追加消息并累计 session.total_tokens。"""
    session = _make_session(db_session, title="消息测试会话")
    assert session.total_tokens == 0

    # user 消息（不计 completion）
    m1 = add_message(
        db_session,
        session_id=session.id,
        role="user",
        content="帮我分析 600010 的近期走势",
        prompt_tokens=50,
        completion_tokens=0,
        latency_ms=10,
        model_used="gpt-4o-mini",
        provider_used="openai",
    )

    assert m1.id is not None
    assert m1.session_id == session.id
    assert m1.role == "user"
    assert m1.content == "帮我分析 600010 的近期走势"
    assert m1.prompt_tokens == 50
    assert m1.completion_tokens == 0
    assert m1.total_tokens == 50
    assert m1.latency_ms == 10
    assert m1.model_used == "gpt-4o-mini"
    assert m1.provider_used == "openai"
    assert m1.created_at is not None

    # 刷新会话验证 token 累计
    db_session.refresh(session)
    assert session.total_tokens == 50

    # assistant 消息（同时计 prompt + completion）
    m2 = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content="600010 近期走势偏强，量价齐升...",
        prompt_tokens=80,
        completion_tokens=200,
        latency_ms=1500,
        model_used="gpt-4o-mini",
        provider_used="openai",
        metadata_json='{"finish_reason":"stop"}',
    )
    assert m2.total_tokens == 280

    db_session.refresh(session)
    assert session.total_tokens == 50 + 280  # 330


# ----------------------------------------------------------------------------
# 4. CRUD 服务：列表查询
# ----------------------------------------------------------------------------


def test_list_sessions(db_session):
    """【WP-AI.1】list_sessions 默认仅返回 active，包含归档需显式开启。"""
    s1 = _make_session(db_session, title="活跃会话-1")
    s2 = _make_session(db_session, title="活跃会话-2")
    s3 = _make_session(db_session, title="归档会话-1")
    archive_session(db_session, s3.id)
    s4 = _make_session(db_session, title="已删除会话-1")
    delete_session(db_session, s4.id)

    # 默认：仅 active
    sessions = list_sessions(db_session, limit=50)
    titles = {s.title for s in sessions}
    assert "活跃会话-1" in titles
    assert "活跃会话-2" in titles
    assert "归档会话-1" not in titles
    assert "已删除会话-1" not in titles

    # include_archived=True：含归档，仍不含已删除
    sessions_with_archived = list_sessions(db_session, limit=50, include_archived=True)
    titles_with_archived = {s.title for s in sessions_with_archived}
    assert "活跃会话-1" in titles_with_archived
    assert "活跃会话-2" in titles_with_archived
    assert "归档会话-1" in titles_with_archived
    assert "已删除会话-1" not in titles_with_archived


# ----------------------------------------------------------------------------
# 5. CRUD 服务：归档
# ----------------------------------------------------------------------------


def test_archive_session(db_session):
    """【WP-AI.1】archive_session 将状态改为 archived，幂等。"""
    session = _make_session(db_session, title="待归档")
    assert session.status == "active"

    archived = archive_session(db_session, session.id)
    assert archived.status == "archived"
    assert archived.id == session.id

    # 幂等：再次归档不报错
    archived_again = archive_session(db_session, session.id)
    assert archived_again.status == "archived"


def test_archive_session_not_found(db_session):
    """【WP-AI.1】归档不存在的会话抛 SessionNotFoundError。"""
    with pytest.raises(SessionNotFoundError):
        archive_session(db_session, 999999)


# ----------------------------------------------------------------------------
# 6. CRUD 服务：软删除
# ----------------------------------------------------------------------------


def test_soft_delete_session(db_session):
    """【WP-AI.1】delete_session 设置 deleted_at，且 get_session 返回 None。"""
    session = _make_session(db_session, title="待删除")
    assert session.status == "active"
    assert session.deleted_at is None

    deleted = delete_session(db_session, session.id)
    assert deleted.status == "deleted"
    assert deleted.deleted_at is not None

    # get_session 不返回已软删除的会话
    assert get_session(db_session, session.id) is None

    # list_sessions 不返回已软删除的会话
    sessions = list_sessions(db_session, limit=50, include_archived=True)
    assert all(s.status != "deleted" for s in sessions)
    assert all(s.id != session.id for s in sessions)


def test_soft_delete_session_not_found(db_session):
    """【WP-AI.1】软删除不存在的会话抛 SessionNotFoundError。"""
    with pytest.raises(SessionNotFoundError):
        delete_session(db_session, 999999)


# ----------------------------------------------------------------------------
# 7. CRUD 服务：动作审计 - 添加
# ----------------------------------------------------------------------------


def test_add_action_audit(db_session):
    """【WP-AI.1】add_action_audit 添加审计记录，支持 dict 自动序列化。"""
    session = _make_session(db_session, title="动作审计测试")
    msg = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content="建议添加 RSI 指标",
    )

    # 使用 dict 作为 payload，应自动序列化为 JSON
    audit = add_action_audit(
        db_session,
        message_id=msg.id,
        action_type="draft_indicator",
        suggested_payload={"indicator": "RSI", "params": {"period": 14}},
        preview_result={"preview": "RSI(14) 即将下穿 30"},
    )

    assert audit.id is not None
    assert audit.message_id == msg.id
    assert audit.action_type == "draft_indicator"
    assert audit.user_confirmed is False
    assert audit.confirmed_at is None
    assert audit.final_result is None
    assert audit.rejected_reason is None
    assert audit.created_at is not None

    # 验证 JSON 序列化
    import json
    parsed = json.loads(audit.suggested_payload)
    assert parsed["indicator"] == "RSI"
    parsed_preview = json.loads(audit.preview_result)
    assert parsed_preview["preview"] == "RSI(14) 即将下穿 30"


# ----------------------------------------------------------------------------
# 8. CRUD 服务：动作审计 - 确认
# ----------------------------------------------------------------------------


def test_confirm_action(db_session):
    """【WP-AI.1】confirm_action 设置 user_confirmed=True 并记录 final_result。"""
    session = _make_session(db_session, title="确认动作测试")
    msg = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content="建议下单价 10.5",
    )
    audit = add_action_audit(
        db_session,
        message_id=msg.id,
        action_type="draft_order",
        suggested_payload='{"side":"buy","price":10.5}',
    )

    assert audit.user_confirmed is False

    confirmed = confirm_action(
        db_session,
        audit.id,
        final_result={"order_id": 12345, "status": "filled"},
    )

    assert confirmed.user_confirmed is True
    assert confirmed.confirmed_at is not None
    assert confirmed.final_result is not None
    assert confirmed.rejected_reason is None

    # 读回验证
    db_session.refresh(audit)
    assert audit.user_confirmed is True
    import json
    parsed = json.loads(audit.final_result)
    assert parsed["order_id"] == 12345


def test_confirm_action_not_found(db_session):
    """【WP-AI.1】确认不存在的审计记录抛 ActionAuditNotFoundError。"""
    with pytest.raises(ActionAuditNotFoundError):
        confirm_action(db_session, 999999)


# ----------------------------------------------------------------------------
# 9. CRUD 服务：动作审计 - 拒绝
# ----------------------------------------------------------------------------


def test_reject_action(db_session):
    """【WP-AI.1】reject_action 设置 rejected_reason 并清空 final_result。"""
    session = _make_session(db_session, title="拒绝动作测试")
    msg = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content="建议创建提醒",
    )
    audit = add_action_audit(
        db_session,
        message_id=msg.id,
        action_type="draft_alert",
        suggested_payload='{"alert_type":"price"}',
    )

    rejected = reject_action(db_session, audit.id, reason="价格已突破，无需提醒")

    assert rejected.user_confirmed is False
    assert rejected.confirmed_at is not None
    assert rejected.rejected_reason == "价格已突破，无需提醒"
    assert rejected.final_result is None

    # 读回验证
    db_session.refresh(audit)
    assert audit.rejected_reason == "价格已突破，无需提醒"


def test_reject_action_not_found(db_session):
    """【WP-AI.1】拒绝不存在的审计记录抛 ActionAuditNotFoundError。"""
    with pytest.raises(ActionAuditNotFoundError):
        reject_action(db_session, 999999)


# ----------------------------------------------------------------------------
# 10. SQLite 补丁幂等
# ----------------------------------------------------------------------------


def test_sqlite_patch_idempotent(db_session):
    """【WP-AI.1】SQLite 补丁函数多次调用幂等，不报错。"""
    engine = db_session.bind
    # 表已由 create_all 创建
    _ensure_sqlite_ai_session_tables(engine)
    # 再次调用不应报错
    _ensure_sqlite_ai_session_tables(engine)
    _ensure_sqlite_ai_session_tables(engine)

    inspector = inspect(engine)
    assert "ai_sessions" in inspector.get_table_names()
    assert "ai_messages" in inspector.get_table_names()
    assert "ai_action_audits" in inspector.get_table_names()


def test_sqlite_patch_creates_table_when_missing():
    """【WP-AI.1】表不存在时 SQLite 补丁创建表。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_ai_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # create_all 后手动 drop 三张表模拟"表不存在"场景
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            # 必须先 drop 子表，再 drop 父表（外键约束）
            conn.execute(text("DROP TABLE IF EXISTS ai_action_audits"))
            conn.execute(text("DROP TABLE IF EXISTS ai_messages"))
            conn.execute(text("DROP TABLE IF EXISTS ai_sessions"))

        inspector = inspect(engine)
        assert "ai_sessions" not in inspector.get_table_names()
        assert "ai_messages" not in inspector.get_table_names()
        assert "ai_action_audits" not in inspector.get_table_names()

        # 调用补丁函数
        _ensure_sqlite_ai_session_tables(engine)

        # 表应已创建
        inspector = inspect(engine)
        assert "ai_sessions" in inspector.get_table_names()
        assert "ai_messages" in inspector.get_table_names()
        assert "ai_action_audits" in inspector.get_table_names()

        # 再次调用（幂等）
        _ensure_sqlite_ai_session_tables(engine)
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 11. MySQL 补丁函数存在且可调用
# ----------------------------------------------------------------------------


def test_mysql_patch_function_exists():
    """【WP-AI.1】_ensure_mysql_ai_session_tables 函数存在且签名正确。

    不需要真实 MySQL 实例，仅验证函数定义存在且可被调用。
    """
    assert callable(_ensure_mysql_ai_session_tables)
    sig = _inspect.signature(_ensure_mysql_ai_session_tables)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"


# ----------------------------------------------------------------------------
# 12. 级联删除（删会话级联删消息；删消息级联删审计）
# ----------------------------------------------------------------------------


def test_cascade_delete_session_to_messages(db_session):
    """【WP-AI.1】物理删除 AISession 级联删除其 AIMessage（ondelete=CASCADE）。

    SQLite 默认不启用外键约束，需通过 PRAGMA foreign_keys=ON 开启
    才能触发数据库级 ON DELETE CASCADE。
    """
    session = _make_session(db_session, title="级联删除测试")
    add_message(
        db_session, session_id=session.id, role="user", content="问题1"
    )
    add_message(
        db_session, session_id=session.id, role="assistant", content="回答1"
    )

    # 在删除前缓存 ID（删除后访问 ORM 实例属性会触发懒加载失败）
    session_id = session.id
    db_session.expunge_all()

    # 开启 SQLite 外键约束以触发 ON DELETE CASCADE
    engine = db_session.bind
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("DELETE FROM ai_sessions WHERE id = :sid"), {"sid": session_id})

    # 验证消息也被级联删除
    from sqlalchemy import select as sa_select
    msgs = db_session.execute(
        sa_select(AIMessage).where(AIMessage.session_id == session_id)
    ).scalars().all()
    assert len(msgs) == 0


def test_cascade_delete_message_to_action_audits(db_session):
    """【WP-AI.1】物理删除 AIMessage 级联删除其 AIActionAudit。

    SQLite 默认不启用外键约束，需通过 PRAGMA foreign_keys=ON 开启
    才能触发数据库级 ON DELETE CASCADE。
    """
    session = _make_session(db_session, title="级联删除审计测试")
    msg = add_message(
        db_session, session_id=session.id, role="assistant", content="建议1"
    )
    add_action_audit(
        db_session,
        message_id=msg.id,
        action_type="draft_indicator",
        suggested_payload='{"indicator":"MACD"}',
    )

    # 在删除前缓存 ID（删除后访问 ORM 实例属性会触发懒加载失败）
    msg_id = msg.id
    db_session.expunge_all()

    # 开启 SQLite 外键约束以触发 ON DELETE CASCADE
    engine = db_session.bind
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("DELETE FROM ai_messages WHERE id = :mid"), {"mid": msg_id})

    # 验证审计也被级联删除
    from sqlalchemy import select as sa_select
    audits = db_session.execute(
        sa_select(AIActionAudit).where(AIActionAudit.message_id == msg_id)
    ).scalars().all()
    assert len(audits) == 0


# ----------------------------------------------------------------------------
# 13. 字段完整性
# ----------------------------------------------------------------------------


def test_ai_sessions_columns(db_session):
    """【WP-AI.1】ai_sessions 表包含所有声明字段。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ai_sessions")}
    expected = {
        "id",
        "title",
        "source_page",
        "provider",
        "model",
        "profile_id",
        "status",
        "context_summary",
        "total_tokens",
        "total_cost",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


def test_ai_messages_columns(db_session):
    """【WP-AI.1】ai_messages 表包含所有声明字段。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ai_messages")}
    expected = {
        "id",
        "session_id",
        "role",
        "content",
        "context_summary",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
        "model_used",
        "provider_used",
        "metadata_json",
        "created_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


def test_ai_action_audits_columns(db_session):
    """【WP-AI.1】ai_action_audits 表包含所有声明字段。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("ai_action_audits")}
    expected = {
        "id",
        "message_id",
        "action_type",
        "suggested_payload",
        "preview_result",
        "user_confirmed",
        "confirmed_at",
        "final_result",
        "rejected_reason",
        "created_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"
