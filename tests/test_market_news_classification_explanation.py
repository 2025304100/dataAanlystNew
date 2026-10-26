"""Current classification explanations and bounded negation via HTTP APIs."""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from app.api.routes.market_events import router
from app.db.session import get_db
from app.services import market_events

pytestmark = pytest.mark.whitebox


@pytest.fixture
def client(db_session):
    app=FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db]=lambda:db_session
    with TestClient(app) as api:
        yield api


def test_new_collection_excludes_explicitly_cancelled_sanction_and_explains(client,monkeypatch):
    monkeypatch.setattr(market_events.ak,"stock_info_global_em",lambda:pd.DataFrame([
        {"title":"美国取消制裁","date":datetime.now()}]))
    result=client.post("/market-events/collect",json={"sources":["eastmoney-global"]})
    assert result.json()["collected"]==1
    event=client.get("/market-events").json()["events"][0]
    assert (event["impact_scope"],event["importance_level"],event["sentiment"])==("international",2,"neutral")
    analysis=event["classification_analysis"]
    assert analysis["scope_hits"]["international"]==["美国"]
    assert "取消制裁" in analysis["negated_phrases"]
    assert analysis["negative_hits"]==[] and analysis["matches_saved"] is True


@pytest.mark.parametrize("title,level,direction",[
    ("美国宣布新制裁",5,"negative"),
    ("美国取消制裁",2,"neutral"),
    ("美国并未取消制裁",5,"negative"),
    ("美国没有解除制裁",5,"negative"),
    ("美国取消对华制裁",2,"neutral"),
    ("美国取消制裁，但随后实施新的制裁",5,"negative"),
    ("系统性风险明显下降",2,"neutral"),
    ("未发生系统性风险",2,"neutral"),
    ("不存在违约风险",2,"neutral"),
    ("制裁风险上升",5,"negative"),
    ("利好增长",2,"positive"),
    ("并无增长",2,"neutral"),
    ("无法避免风险",2,"negative"),
])
def test_public_collection_negation_boundaries(client,monkeypatch,title,level,direction):
    monkeypatch.setattr(market_events.ak,"stock_info_global_em",lambda:pd.DataFrame([
        {"title":"规则验收："+title,"date":datetime.now()}]))
    assert client.post("/market-events/collect",json={"sources":["eastmoney-global"]}).json()["collected"]==1
    event=client.get("/market-events").json()["events"][0]
    assert (event["importance_level"],event["sentiment"])==(level,direction)


def test_historical_saved_labels_are_not_rewritten_by_current_analysis(client):
    item=client.post("/market-events",json={"title":"美国取消制裁","importance_level":5,
        "impact_scope":"international","sentiment":"negative"}).json()
    first=client.get(f"/market-events/{item['id']}").json()
    assert (first["importance_level"],first["sentiment"])==(5,"negative")
    assert (first["classification_analysis"]["current_level"],first["classification_analysis"]["current_sentiment"])==(2,"neutral")
    assert first["classification_analysis"]["matches_saved"] is False
    second=client.get(f"/market-events/{item['id']}").json()
    assert second==first


def test_scope_tie_and_keyword_counts_are_visible(client,monkeypatch):
    monkeypatch.setattr(market_events.ak,"stock_info_global_em",lambda:pd.DataFrame([
        {"title":"美国宣布制裁","date":datetime.now()}]))
    client.post("/market-events/collect",json={"sources":["eastmoney-global"]})
    analysis=client.get("/market-events").json()["events"][0]["classification_analysis"]
    assert analysis["scope_ties"]==["international","breaking"]
    assert analysis["scope_hits"]["breaking"]==["制裁"]
    assert analysis["level_hits"]["5"]==["制裁"] and analysis["negative_hits"]==["制裁"]
