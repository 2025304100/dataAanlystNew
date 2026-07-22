"""白盒测试 - WP9.6 API 废弃期访问日志与 HTTP 头。

守护 spec 要求：
1. 被标记为 deprecated 的端点响应包含：
   - Deprecation: true
   - Sunset: 2026-12-31
   - Link: </api/v1/portfolios/{portfolio_id}/members>; rel="successor-version"
2. 访问日志写入 api_deprecation_logs 表（best-effort，失败不阻断请求）
3. 访问日志可通过 query_deprecation_logs / count_deprecation_logs 查询

测试覆盖：
- test_deprecated_api_returns_deprecation_header：访问 /api/v1/scans/latest/executable
  响应头包含 Deprecation/Sunset/Link
- test_deprecation_log_recorded：访问后 api_deprecation_logs 表新增一条记录
- test_deprecation_log_queryable：query_deprecation_logs / count_deprecation_logs 可查

注意：
- 中间件使用 DatabaseManager 的 session_factory（与 db_session fixture 共享同一 engine）
- 测试需用 monkeypatch 清理环境变量避免污染
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.middleware.deprecation_log import (
    count_deprecation_logs,
    query_deprecation_logs,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================


def _override_db(db_session):
    """返回一个覆盖 get_db 依赖的函数。"""
    def override_get_db():
        yield db_session
    return override_get_db


# ============================================================================
# WP9.6 废弃 API 头 + 访问日志
# ============================================================================


class TestWP96DeprecationHeaderAndLog:
    """WP9.6：废弃端点响应头 + 访问日志记录与查询。"""

    def test_deprecated_api_returns_deprecation_header(self, db_session, monkeypatch):
        """【WP9.6】访问 /api/v1/scans/latest/executable 响应包含废弃头。

        验证点：
        1. 响应头包含 Deprecation: true
        2. 响应头包含 Sunset: 2026-12-31
        3. 响应头包含 Link 且 rel="successor-version"
        4. 旧 API 在废弃期仍可正常访问（返回 200，不返回 410/404）
        """
        from app.main import app

        # 清除可能干扰的环境变量
        monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", raising=False)

        app.dependency_overrides[get_db] = _override_db(db_session)
        client = TestClient(app)
        try:
            resp = client.get("/api/v1/scans/latest/executable")

            # 旧 API 仍可正常访问（无 ScanRun 时返回 200 + 空列表）
            assert resp.status_code == 200, (
                f"WP9.6: 废弃端点在 Sunset 前应可正常访问，实际 status={resp.status_code}"
            )

            # 验证废弃相关 HTTP 头
            headers = resp.headers
            assert headers.get("Deprecation") == "true", (
                f"WP9.6: Deprecation 头应为 'true'，实际为 {headers.get('Deprecation')!r}"
            )
            assert headers.get("Sunset") == "2026-12-31", (
                f"WP9.6: Sunset 头应为 '2026-12-31'，实际为 {headers.get('Sunset')!r}"
            )
            link_header = headers.get("Link", "")
            assert 'rel="successor-version"' in link_header, (
                f"WP9.6: Link 头应包含 rel=\"successor-version\"，实际为 {link_header!r}"
            )
            assert "/api/v1/portfolios/" in link_header, (
                f"WP9.6: Link 头应指向组合成员管理端点，实际为 {link_header!r}"
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_deprecation_log_recorded(self, db_session, monkeypatch):
        """【WP9.6】访问废弃端点后 api_deprecation_logs 表新增一条记录。

        验证点：
        1. 访问前 api_deprecation_logs 为空（或记录基线计数）
        2. 访问 /api/v1/scans/latest/executable 后计数 +1
        3. 记录的字段包含 endpoint / method / successor_endpoint / sunset_date
        """
        from app.main import app
        from app.models.api_deprecation_log import ApiDeprecationLog

        monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", raising=False)

        # 访问前基线
        baseline_count = db_session.query(ApiDeprecationLog).count()

        app.dependency_overrides[get_db] = _override_db(db_session)
        client = TestClient(app)
        try:
            resp = client.get("/api/v1/scans/latest/executable")
            assert resp.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)

        # 中间件使用独立 session 写入并 commit，测试 session 需要重新查询
        # db_session 可能存在缓存，先 expire
        db_session.expire_all()

        after_count = db_session.query(ApiDeprecationLog).count()
        assert after_count == baseline_count + 1, (
            f"WP9.6: 访问废弃端点后日志数应 +1，"
            f"baseline={baseline_count}, after={after_count}"
        )

        # 验证最新记录字段
        latest = (
            db_session.query(ApiDeprecationLog)
            .order_by(ApiDeprecationLog.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.endpoint == "/api/v1/scans/latest/executable"
        assert latest.method == "GET"
        assert latest.successor_endpoint == "/api/v1/portfolios/{portfolio_id}/members"
        assert latest.sunset_date == "2026-12-31"
        assert latest.status_code == 200

    def test_deprecation_log_queryable(self, db_session, monkeypatch):
        """【WP9.6】query_deprecation_logs / count_deprecation_logs 可查询日志。

        验证点：
        1. 多次访问后日志数量累计
        2. query_deprecation_logs 返回按 created_at 降序的列表
        3. count_deprecation_logs 返回总数
        4. 按 endpoint 过滤查询正确
        """
        from app.main import app

        monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", raising=False)

        # 访问前基线
        baseline = count_deprecation_logs(db_session)

        app.dependency_overrides[get_db] = _override_db(db_session)
        client = TestClient(app)
        try:
            # 连续访问 3 次
            for _ in range(3):
                resp = client.get("/api/v1/scans/latest/executable")
                assert resp.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)

        # 刷新缓存
        db_session.expire_all()

        # 验证 count_deprecation_logs
        total = count_deprecation_logs(db_session)
        assert total == baseline + 3, (
            f"WP9.6: 3 次访问后日志总数应 +3，baseline={baseline}, total={total}"
        )

        # 验证按 endpoint 过滤
        endpoint_count = count_deprecation_logs(
            db_session, endpoint="/api/v1/scans/latest/executable"
        )
        assert endpoint_count == baseline + 3

        # 验证不存在的 endpoint 过滤
        nonexistent_count = count_deprecation_logs(
            db_session, endpoint="/api/v1/nonexistent"
        )
        assert nonexistent_count == 0

        # 验证 query_deprecation_logs 返回列表（按 created_at 降序）
        logs = query_deprecation_logs(
            db_session, endpoint="/api/v1/scans/latest/executable", limit=10
        )
        assert len(logs) == baseline + 3
        # 验证降序排列（created_at 递减或 id 递减，新记录 id 更大）
        ids = [log.id for log in logs]
        assert ids == sorted(ids, reverse=True), (
            "WP9.6: query_deprecation_logs 应按 created_at 降序返回"
        )

        # 验证 limit 生效
        limited_logs = query_deprecation_logs(
            db_session, endpoint="/api/v1/scans/latest/executable", limit=2
        )
        assert len(limited_logs) == 2

        # 验证 offset 生效
        offset_logs = query_deprecation_logs(
            db_session,
            endpoint="/api/v1/scans/latest/executable",
            limit=10,
            offset=1,
        )
        assert len(offset_logs) == baseline + 3 - 1
