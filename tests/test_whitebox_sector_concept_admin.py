"""白盒测试 - 行情消息 P2-12/P1-6：板块关键词运营化 + 关键词检索（2026-10-25）。

契约：
- DB 覆盖按 sector_id 整行替换关键词，采集打标与热点聚合立即生效；
- 空关键词列表删除覆盖行，恢复内置；
- 未知 sector_id 可创建自定义板块（仅参与匹配聚合）；
- 列表 keyword 参数过滤标题/摘要（autoescape）。
"""
from __future__ import annotations

import pytest

from app.schemas.market_event import MarketEventCollectRequest
from app.services import market_events as svc
from app.services.sector_concepts import (
    list_sector_concept_configs,
    map_to_sectors_db,
    update_sector_concept_config,
)

pytestmark = pytest.mark.whitebox


def _row(title, summary=None):
    return {"title": title, "summary": summary, "source": "eastmoney-global",
            "source_url": None, "published_at": svc._now()}


def test_db_keyword_override_changes_matching(db_session):
    # 内置词不含"狗狗币" → 默认无板块
    assert map_to_sectors_db(db_session, "狗狗币单日暴涨", None) == []
    update_sector_concept_config(db_session, "crypto_web3", ["狗狗币", "柴犬币"], updated_by="tester")
    assert map_to_sectors_db(db_session, "狗狗币单日暴涨", None) == ["crypto_web3"]

    # 采集打标路径同步生效
    import unittest.mock as mock

    with mock.patch.object(svc, "_fetch_global_news", return_value=[_row("狗狗币单日暴涨创纪录")]):
        result = svc.collect_market_events(
            db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"])
        )
    assert result.collected == 1
    row = db_session.query(svc.MarketEvent).order_by(svc.MarketEvent.id.desc()).first()
    assert row.affected_sectors == "crypto_web3"


def test_empty_keywords_restores_builtin(db_session):
    update_sector_concept_config(db_session, "solar_pv", ["光伏", "自定义词甲"], updated_by="tester")
    configs = {c["sector_id"]: c for c in list_sector_concept_configs(db_session)}
    assert configs["solar_pv"]["source"] == "db"
    assert "自定义词甲" in configs["solar_pv"]["keywords"]

    result = update_sector_concept_config(db_session, "solar_pv", [], updated_by="tester")
    assert result["source"] == "builtin"
    configs = {c["sector_id"]: c for c in list_sector_concept_configs(db_session)}
    assert configs["solar_pv"]["source"] == "builtin"
    assert "自定义词甲" not in configs["solar_pv"]["keywords"]


def test_custom_sector_id_allowed(db_session):
    update_sector_concept_config(db_session, "my_custom_sector", ["冷门概念词乙"])
    assert map_to_sectors_db(db_session, "冷门概念词乙登上热搜", None) == ["my_custom_sector"]
    configs = {c["sector_id"]: c for c in list_sector_concept_configs(db_session)}
    assert configs["my_custom_sector"]["source"] == "db"


def test_invalid_sector_id_rejected(db_session):
    with pytest.raises(ValueError):
        update_sector_concept_config(db_session, "  ", ["词"])


def test_list_keyword_filter(db_session):
    import hashlib

    def _mk(title, summary):
        db_session.add(svc.MarketEvent(
            title=title, summary=summary, impact_scope="other", importance_level=2,
            affected_market="A股/期货", sentiment="neutral", source="test", is_manual=0,
            published_at=svc._now(), expires_at=svc._now(),
            title_hash=hashlib.md5(title.encode()).hexdigest(),
        ))
    _mk("国务院发布新能源产业规划", None)
    _mk("美联储公布利率决议", None)
    _mk("某公司年度总结", "摘要里提到新能源车补贴")
    db_session.commit()

    result = svc.list_market_events(db_session, keyword="新能源")
    assert result.total == 2  # 标题 1 + 摘要 1

    # 通配符不生效（autoescape）
    result2 = svc.list_market_events(db_session, keyword="%")
    assert result2.total == 0
