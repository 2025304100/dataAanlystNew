"""Deterministic HTTP/time fault replay; no public supplier or business DB."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse
from collections import Counter
from datetime import date,timedelta
import importlib
import json
from unittest.mock import patch

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db.base import Base
from app.models.symbol import Symbol
from app.models.capital_flow import CapitalFlow
from app.models.akshare_api_config import AkshareApiConfig
from app.services import capital_flow_data as flow, capital_flow_history_provider as provider
from app.services import akshare_registry as registry, akshare_utils


class Clock:
    def __init__(self):self.elapsed=0.0
    def monotonic(self):return self.elapsed
    def time(self):return 1793066400+self.elapsed
    def sleep(self,seconds):self.elapsed+=seconds


def replay(scenario,count):
    importlib.reload(provider)
    engine=create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine,tables=[model.__table__ for model in (Symbol,CapitalFlow,AkshareApiConfig)])
    day=date.today()-timedelta(days=1)
    clock=Clock()
    received=[]
    per_symbol=Counter()
    class HTTP:
        trust_env=True
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            secid=kwargs["params"]["secid"]
            market,code=secid.split(".")
            per_symbol[code]+=1
            received.append({"symbol":code,"at":clock.elapsed})
            if scenario=="persistent_reset":
                raise requests.exceptions.ConnectionError("simulated persistent remote reset")
            if scenario=="transient_timeout" and per_symbol[code]==1:
                clock.sleep(15)  # Include the actual read-timeout cost in this replay.
                raise requests.exceptions.ReadTimeout("simulated transient timeout")
            status=503 if scenario=="transient_503" and per_symbol[code]==1 else 200
            response=requests.Response()
            response.status_code=status
            response._content=json.dumps({"rc":0,"data":{"code":code,"market":int(market),
                "klines":[f"{day},100,-20,-80,40,60,1,-.2,-.8,.4,.6,10,1,0,0"]}}).encode()
            return response
    completed=0
    errors=Counter()
    with Session(engine) as db:
        symbols=[Symbol(symbol=f"{600000+i}",name="replay",market="sh",asset_type="stock") for i in range(count)]
        db.add_all(symbols)
        db.add(AkshareApiConfig(api_key="stock_individual_fund_flow",enabled=True,anti_risk_strategy="custom",
                               delay_min_ms=0,delay_max_ms=0))
        db.commit()
        registry.load_config_cache(db)
        with patch.object(provider,"time",clock),patch.object(akshare_utils,"time",clock),patch.object(registry,"time",clock),patch.object(provider.requests,"Session",HTTP):
            for symbol in symbols:
                try:
                    count_written=flow.sync_symbol_capital_flow_range(db,symbol,start_date=day,end_date=day)
                    db.commit()
                    completed+=int(count_written==1)
                except Exception as exc:
                    db.rollback()
                    errors[type(exc).__name__]+=1
    engine.dispose()
    return {"scenario":scenario,"symbols":count,"succeeded":completed,"failed":count-completed,
            "http_requests":len(received),"simulated_wait_seconds":round(clock.elapsed,4),
            "errors":dict(errors),"http_timeline":received}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result={"scope":"deterministic supplier faults and virtual clock, not production performance",
            "external_network":False,"model_calls":0,"notification_sends":0,
            "scenarios":[replay(name,5) for name in ("healthy","transient_timeout","transient_503","persistent_reset")]}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"scenarios":[{k:v for k,v in row.items() if k!="http_timeline"} for row in result["scenarios"]]},ensure_ascii=False))


if __name__=="__main__":main()
