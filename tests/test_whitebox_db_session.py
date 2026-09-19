"""app/db/session.py 白盒：控制面缓存失效接口（TD2 收尾裁决）。

覆盖：
- 缓存冻结语义：dm.engine 进程内改绑 URL 后，旧 factory 仍指向旧 URL
  （这正是集成测试顺序污染的根因，先钉住它）；
- reset_control_plane_cache()：置空缓存、返回旧 engine、锁外 dispose；
  下一次 get_control_session_local() 基于当前 dm.engine.url 重建；
- 幂等安全：缓存为空时 reset 返回 None 不抛；连续 reset 可重复。

全部用 tmp sqlite URL，不触真实库；dm 单例字段与 cp 缓存 save/restore 隔离。
"""
from __future__ import annotations

from sqlalchemy import create_engine

import app.db.session as db_session
from app.db.manager import DatabaseManager


def _tmp_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_reset_control_plane_cache_rebuilds_from_current_engine(tmp_path):
    mgr = DatabaseManager.get()
    saved_mgr = (mgr._engine, mgr._session_factory, mgr._db_type)
    saved_cp = (db_session._cp_engine, db_session._cp_factory)
    extra_engines = []
    try:
        url_a = _tmp_url(tmp_path / "cp_a.sqlite3")
        url_b = _tmp_url(tmp_path / "cp_b.sqlite3")
        eng_a = create_engine(url_a)
        eng_b = create_engine(url_b)
        extra_engines.extend([eng_a, eng_b])

        mgr._engine = eng_a
        mgr._db_type = "sqlite"
        mgr._session_factory = None

        factory_a = db_session.get_control_session_local()
        assert factory_a is not None
        assert str(factory_a.kw["bind"].url) == url_a

        # 进程内改绑 dm.engine 后、未失效缓存：控制面仍冻结在旧 URL（根因语义）
        mgr._engine = eng_b
        frozen = db_session.get_control_session_local()
        assert frozen is factory_a
        assert str(frozen.kw["bind"].url) == url_a

        # 官方失效接口：返回旧引擎 + 置空缓存
        stale = db_session.reset_control_plane_cache()
        assert stale is factory_a.kw["bind"]
        assert db_session._cp_engine is None
        assert db_session._cp_factory is None

        factory_b = db_session.get_control_session_local()
        assert factory_b is not factory_a
        assert str(factory_b.kw["bind"].url) == url_b
        extra_engines.append(factory_b.kw["bind"])
    finally:
        mgr._engine, mgr._session_factory, mgr._db_type = saved_mgr
        db_session._cp_engine, db_session._cp_factory = saved_cp
        for e in extra_engines:
            try:
                e.dispose()
            except Exception:
                pass


def test_reset_control_plane_cache_is_safe_when_cache_empty(tmp_path):
    mgr = DatabaseManager.get()
    saved_mgr = (mgr._engine, mgr._session_factory, mgr._db_type)
    saved_cp = (db_session._cp_engine, db_session._cp_factory)
    extra_engines = []
    try:
        mgr._engine = create_engine(_tmp_url(tmp_path / "cp_c.sqlite3"))
        mgr._db_type = "sqlite"
        mgr._session_factory = None
        db_session._cp_engine = None
        db_session._cp_factory = None

        # 空缓存时 reset：返回 None、不抛
        assert db_session.reset_control_plane_cache() is None

        # 空缓存时 get 可正常重建
        factory = db_session.get_control_session_local()
        assert factory is not None
        extra_engines.append(factory.kw["bind"])

        # 连续 reset：第二次失效刚建的引擎，缓存归空
        stale = db_session.reset_control_plane_cache()
        assert stale is factory.kw["bind"]
        assert db_session._cp_factory is None
    finally:
        mgr._engine, mgr._session_factory, mgr._db_type = saved_mgr
        db_session._cp_engine, db_session._cp_factory = saved_cp
        for e in extra_engines:
            try:
                e.dispose()
            except Exception:
                pass
