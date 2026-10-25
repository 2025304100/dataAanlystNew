"""HTTP acceptance helpers: append input rows and read complete financial state."""
from datetime import date, datetime, timedelta
import json
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from tests.combination_auto_support import wait_task


def seed_score_history(engine):
    from app.models.score import Score
    with Session(engine) as db:
        for index in range(32):
            stamp=datetime(2024,1,2)+timedelta(days=index)
            db.add(Score(symbol_id=1,trade_date=stamp.date(),quality_score=70,quality_grade='B',
                timing_score=70,stage='neutral',action='hold',priority_score=70,weight_mode='manual',
                calc_batch_id=f'preserve-historical-{index:02d}',created_at=stamp,published_at=stamp))
        db.commit()


def score_rows(engine, maximum=None):
    with engine.connect() as conn:
        maximum=maximum if maximum is not None else conn.scalar(text('SELECT COALESCE(MAX(id),0) FROM scores'))
        return [dict(r) for r in conn.execute(text('SELECT * FROM scores WHERE id<=:maximum ORDER BY id'),{'maximum':maximum}).mappings()]


def append_market_day(engine, day_text):
    from app.models.daily_bar import DailyBar
    from app.models.universe import UniverseSymbol, UniverseDailyBar
    from app.models.stock_valuation import StockValuation
    day=date.fromisoformat(day_text)
    observed=datetime.combine(day,datetime.min.time())+timedelta(hours=7)
    with Session(engine) as db:
        for number in range(100):
            sid=number+1
            universe=db.scalar(select(UniverseSymbol).where(UniverseSymbol.symbol==str(600001+number)))
            previous=db.scalar(select(DailyBar).where(DailyBar.symbol_id==sid).order_by(DailyBar.trade_date.desc()).limit(1))
            assert previous.trade_date<day
            old=db.scalar(select(StockValuation).where(StockValuation.symbol_id==sid).order_by(StockValuation.trade_date.desc()).limit(1))
            close=previous.close*(1+.0005*(number-49.5)/50)
            value=dict(trade_date=day,open=previous.close,close=close,high=max(close,previous.close)*1.01,
                low=min(close,previous.close)*.99,volume=1e6,amount=close*1e6,turnover_rate=previous.turnover_rate,
                source='isolated.synthetic.append',created_at=observed)
            db.add(DailyBar(symbol_id=sid,**value))
            db.add(UniverseDailyBar(universe_symbol_id=universe.id,**value))
            db.add(StockValuation(symbol_id=sid,trade_date=day,pe_ttm=old.pe_ttm,pb=old.pb,
                source='isolated.synthetic.append',created_at=observed))
        db.commit()


def financial_rows(engine):
    with engine.connect() as conn:
        return {name:[dict(row) for row in conn.execute(text(f'SELECT * FROM {name} ORDER BY id')).mappings()]
            for name in ('cash_ledger','positions','sim_orders','sim_trades')}


def api_json(client, method, path, body=None, status=200):
    response=client.request(method,'/api/v1'+path,**({'json':body} if body is not None else {}))
    assert response.status_code==status,(path,response.status_code,response.text)
    return response.json()


def publish_scores(client, model_id, set_id, day, evidence, mode='ridge'):
    task=api_json(client,'POST','/scoring/tasks',{'train_model':False,'materialize_scores':True,
        'factor_set_id':set_id,'start_date':day,'end_date':day,'actor':'isolated-e2e',
        'hyperparameters':{'expected_model_run_id':model_id,'expected_weight_mode':mode}})
    done=wait_task(client,task['id'])
    (evidence/f'scores-{day}-{mode}.json').write_text(json.dumps(done,default=str,indent=2),encoding='utf8')
    assert done['status']=='done',done
    return done
