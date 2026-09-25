"""白盒测试 - DEF-6：API 路径不存在时的错误信封必须**中文 + BFG 结构**。

缺陷背景（2026-09-24 全面测试报告 DEF-6）
----------------------------------------
`app/main.py` 的 SPA 兜底路由对任何未匹配的 `api/*` 路径抛
`HTTPException(404, detail="Not found")` —— 裸字符串经全局处理器原样透出：

    {"error_code": "NOT_FOUND", "user_message": "Not found", ...}

用户看到的 `user_message` 是**英文**，且不属于 BFG 7 要素信封，前端只能按
「未知错误码」降级展示。业务路由的 404（如证据接口候选不存在）反而是
标准中文信封 —— 同一个产品里两种信封形态不一致，是这次要根治的点。

验收：未知 API 路径 → 404 + 结构化中文信封，且响应体里**不出现**英文
"Not found" 作为用户可见文案。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db

pytestmark = pytest.mark.whitebox


def _override_db(db_session):
    def override_get_db():
        yield db_session
    return override_get_db


class TestApiNotFoundEnvelope:
    def test_unknown_api_path_returns_chinese_envelope(self, db_session, monkeypatch):
        monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", raising=False)
        from app.main import app

        app.dependency_overrides[get_db] = _override_db(db_session)
        client = TestClient(app)
        resp = client.get("/api/v1/factor-mining/definitely-not-a-route")
        app.dependency_overrides.pop(get_db, None)

        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body.get("error_code") == "NOT_FOUND"
        payload = str(body)
        # ① 用户可见文案必须中文（不得再出现英文 "Not found"）
        assert "Not found" not in payload, f"仍有英文信封残留: {payload[:300]}"
        # ② 结构化：中文标题/明细齐备
        assert "接口不存在" in payload
        assert "definitely-not-a-route" in payload or "未找到接口" in payload
        # ③ 可操作指引（fix_link / 是否可重试）
        assert "retryable" in body

    def test_business_404_stays_chinese(self, db_session, monkeypatch):
        """对照：业务路由的 404（证据接口候选不存在）同样是中文信封。"""
        monkeypatch.delenv("AUTO_TRADE_MEMBER_SOURCE_ENABLED", raising=False)
        from app.main import app

        app.dependency_overrides[get_db] = _override_db(db_session)
        client = TestClient(app)
        resp = client.get(
            "/api/v1/factor-mining/candidates/cand-not-exist-000/grade/evidence")
        app.dependency_overrides.pop(get_db, None)

        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body.get("error_code") == "NOT_FOUND"
        assert "Not found" not in str(body)
        assert "候选" in str(body) or "资源不存在" in str(body)
