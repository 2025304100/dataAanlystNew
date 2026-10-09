"""钉住 `_ensure_cfg_row` 的 select-then-insert 竞态处理。

CI 实测到的现象（blackbox）：
    PUT /external-data/apis/stock_info_a_code_name → 409 DB_INTEGRITY_VIOLATION
    1062 Duplicate entry ... for key 'akshare_api_config.ix_akshare_api_config_api_key'
即 SELECT 说"没有这行"、INSERT 说"这行已存在"。

成因：另一个连接（启动种子 / 后台任务 / 并发请求）刚插入并提交了同一 api_key；
本 session 在 MySQL REPEATABLE READ 下的快照读看不到那条已提交的行，
于是走 INSERT 分支撞唯一索引。SQLite 语义下不容易撞，所以本地长期测不到。

修法要求：竞态时不得把 409 抛给用户，而是取回已存在的那行继续用。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.routes.akshare_apis as api_mod
from app.db.base import Base
from app.models.akshare_api_config import AkshareApiConfig

pytestmark = pytest.mark.whitebox

KEY = "stock_info_a_code_name"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _seed_row(sess) -> None:
    sess.add(AkshareApiConfig(
        api_key=KEY, enabled=True, anti_risk_strategy="standard",
        delay_min_ms=300, delay_max_ms=800,
    ))
    sess.commit()


def test_race_returns_existing_row_instead_of_raising(session, monkeypatch):
    """快照读看不到已提交的行时，必须回退到"用已有行"，不能抛 IntegrityError。"""
    _seed_row(session)
    session.expunge_all()
    # 模拟 REPEATABLE READ 下的陈旧快照：普通 SELECT 查不到已存在的行
    monkeypatch.setattr(api_mod, "_get_cfg_row", lambda db, api_key: None)

    row = api_mod._ensure_cfg_row(session, KEY)

    assert row is not None
    assert row.api_key == KEY
    # 关键：没有插出第二行（撞唯一索引的话这里要么抛异常要么两行）
    n = len(session.execute(select(AkshareApiConfig)).scalars().all())
    assert n == 1, f"竞态处理失败，多插了一行：{n}"


def test_without_fix_the_insert_would_conflict(session, monkeypatch):
    """反向证据：绕开修复逻辑直接 add+flush 必须抛 1062 类错误。

    这条保证上一条测试不是"因为什么都没发生而通过"。
    """
    _seed_row(session)
    session.expunge_all()
    monkeypatch.setattr(api_mod, "_get_cfg_row", lambda db, api_key: None)

    with pytest.raises(IntegrityError):
        session.add(AkshareApiConfig(
            api_key=KEY, enabled=True, anti_risk_strategy="standard",
            delay_min_ms=300, delay_max_ms=800,
        ))
        session.flush()
