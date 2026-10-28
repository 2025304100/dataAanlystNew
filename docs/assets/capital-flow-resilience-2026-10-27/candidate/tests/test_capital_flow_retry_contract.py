"""External HTTP/time fault scripts against the public history adapter."""
import importlib
import json

import pytest
import requests

from app.services import capital_flow_history_provider as provider

pytestmark = pytest.mark.whitebox


class Clock:
    def __init__(self):
        self.value=0.0
    def monotonic(self):
        return self.value
    def time(self):
        return 1793066400+self.value
    def sleep(self,seconds):
        assert seconds>=0
        self.value+=seconds


def body():
    return {"rc":0,"data":{"code":"600000","market":1,
        "klines":["2026-10-26,100,-20,-80,40,60,1,-.2,-.8,.4,.6,10,1,0,0"]}}


@pytest.fixture
def external(monkeypatch):
    # Initialize requests hardening before replacing the external HTTP client.
    from app.services import akshare_utils  # noqa: F401
    importlib.reload(provider)
    clock=Clock()
    monkeypatch.setattr(provider,"time",clock)
    monkeypatch.setenv("CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS","0")
    monkeypatch.setenv("CAPITAL_FLOW_BACKOFF_BASE_SECONDS","1")
    monkeypatch.setenv("CAPITAL_FLOW_BACKOFF_MAX_SECONDS","4")
    monkeypatch.setenv("CAPITAL_FLOW_RETRY_JITTER_RATIO","0")
    monkeypatch.setenv("CAPITAL_FLOW_RETRY_BUDGET_SECONDS","12")
    calls=[]
    def install(script):
        class HTTP:
            trust_env=True
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def get(self,url,**kwargs):
                calls.append({"at":clock.value,"url":url,"timeout":kwargs["timeout"]})
                item=script[min(len(calls)-1,len(script)-1)]
                if isinstance(item,tuple) and item[0]=="elapsed_timeout":
                    clock.sleep(item[1])
                    raise requests.exceptions.ReadTimeout("read timeout after waiting")
                if isinstance(item,Exception):raise item
                status,headers,payload=item
                response=requests.Response()
                response.status_code=status
                response.headers.update(headers)
                response._content=json.dumps(payload).encode()
                return response
        monkeypatch.setattr(provider.requests,"Session",HTTP)
    from app.services import akshare_registry
    cached=akshare_registry.snapshot_config_cache()
    try:
        yield clock,calls,install
    finally:
        akshare_registry.restore_config_cache(cached)


def test_rate_limited_history_recovers_without_retrying_before_retry_after(external):
    clock,calls,install=external
    install([(429,{"Retry-After":"3"},{}),(200,{},body())])
    result=provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert result.iloc[0]["主力净流入-净额"]==100
    assert len(calls)==2 and calls[1]["at"]>=3
    assert clock.value<=12


@pytest.mark.parametrize("failure", [503,requests.exceptions.ReadTimeout("transient read timeout")])
def test_transient_supplier_failure_is_retried_and_validated(external,failure):
    _,calls,install=external
    first=(failure,{}, {}) if isinstance(failure,int) else failure
    install([first,(200,{},body())])
    result=provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert result.iloc[0]["主力净流入-净额"]==100
    assert len(calls)==2


def test_retry_after_http_date_is_honored(external):
    from datetime import datetime,timezone
    from email.utils import format_datetime
    clock,calls,install=external
    until=format_datetime(datetime.fromtimestamp(clock.time()+5,timezone.utc),usegmt=True)
    install([(429,{"Retry-After":until},{}),(200,{},body())])
    assert len(provider.fetch_capital_flow_history(stock="600000",market="sh"))==1
    assert calls[1]["at"]>=5


def test_long_supplier_cooldown_exhausts_budget_without_early_retry(external,monkeypatch):
    clock,calls,install=external
    monkeypatch.setenv("CAPITAL_FLOW_RETRY_BUDGET_SECONDS","5")
    install([(429,{"Retry-After":"20"},{})])
    with pytest.raises(requests.exceptions.Timeout,match="budget"):
        provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert len(calls)==1 and clock.value==5


def test_persistent_timeout_stops_after_three_attempts(external,monkeypatch):
    clock,calls,install=external
    monkeypatch.setenv("CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS","1")
    install([requests.exceptions.ReadTimeout("persistent timeout")])
    with pytest.raises(requests.exceptions.ReadTimeout):
        provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert len(calls)==3 and clock.value==2


@pytest.mark.parametrize("status",[400,401,403,404])
def test_permanent_http_failure_is_not_retried(external,status):
    clock,calls,install=external
    install([(status,{}, {})])
    with pytest.raises(requests.exceptions.HTTPError):
        provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert len(calls)==1 and clock.value==0


@pytest.mark.parametrize("damage",["identity","fields","duplicate","failure_code"])
def test_invalid_success_payload_never_becomes_a_retry_success(external,damage):
    _,calls,install=external
    payload=body()
    if damage=="identity":payload["data"]["code"]="600001"
    if damage=="fields":payload["data"]["klines"][0]=payload["data"]["klines"][0].replace(",100,",",nan,")
    if damage=="duplicate":payload["data"]["klines"]*=2
    if damage=="failure_code":payload["rc"]=1
    install([(200,{},payload)])
    with pytest.raises(ValueError):
        provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert len(calls)==1


def test_healthy_requests_keep_the_existing_fifteen_second_floor(external,monkeypatch):
    _,calls,install=external
    monkeypatch.setenv("CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS","15")
    monkeypatch.setenv("CAPITAL_FLOW_RETRY_BUDGET_SECONDS","75")
    install([(200,{},body())])
    for _ in range(3):
        assert len(provider.fetch_capital_flow_history(stock="600000",market="sh"))==1
    assert [entry["at"] for entry in calls]==[0,15,30]


def test_disabling_source_during_cooldown_prevents_next_request(external,db_session,monkeypatch):
    from app.models.akshare_api_config import AkshareApiConfig
    from app.services import akshare_registry
    from app.services.akshare_utils import AkshareApiDisabledError
    clock,calls,install=external
    row=AkshareApiConfig(api_key="stock_individual_fund_flow",enabled=True,anti_risk_strategy="standard",
                        delay_min_ms=0,delay_max_ms=0)
    db_session.add(row)
    db_session.commit()
    akshare_registry.load_config_cache(db_session)
    original=clock.sleep
    def sleep(seconds):
        original(seconds)
        row.enabled=False
        db_session.commit()
        akshare_registry.refresh_config_cache_for("stock_individual_fund_flow",db_session)
    monkeypatch.setattr(clock,"sleep",sleep)
    install([(503,{},{}),(200,{},body())])
    with pytest.raises(AkshareApiDisabledError):
        provider.fetch_capital_flow_history(stock="600000",market="sh")
    assert len(calls)==1


def test_time_already_spent_in_read_timeout_is_not_waited_again(external,monkeypatch):
    clock,calls,install=external
    for key in ["CAPITAL_FLOW_BACKOFF_BASE_SECONDS","CAPITAL_FLOW_BACKOFF_MAX_SECONDS"]:
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv("CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS","15")
    monkeypatch.setenv("CAPITAL_FLOW_RETRY_BUDGET_SECONDS","75")
    install([("elapsed_timeout",15),(200,{},body())])
    assert len(provider.fetch_capital_flow_history(stock="600000",market="sh"))==1
    assert [entry["at"] for entry in calls]==[0,15]
