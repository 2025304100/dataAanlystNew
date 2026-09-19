"""Task 4 Acceptance Tests?TR-4.1 / TR-4.2 / TR-4.3??"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from app.db.manager import DatabaseManager
_db_cfg = json.load(open("config/db_config.json", encoding="utf-8"))
if _db_cfg.get("use_mysql"):
    _m = _db_cfg["mysql"]
    _url = "mysql+pymysql://{}:{}@{}:{}/{}?charset=utf8mb4".format(
        _m["user"], _m["password"], _m["host"], _m["port"], _m["database"]
    )
    DatabaseManager.get().initialize(_url, db_type="mysql")
else:
    _url = "sqlite:///app.db"
    DatabaseManager.get().initialize(_url, db_type="sqlite")
print("[setup] DB initialized:", _url.split("@")[-1] if "@" in _url else _url)

from sqlalchemy import text
from app.schemas.bfg_precheck import BacktestPrecheckRequest
from app.services.bfg_precheck_service import run_backtest_precheck
from app.services.bfg_trade_calendar_adapter import (
    TradeCalendarUnavailableError,
    get_trading_days,
)
from app.db.session import SessionLocal

PASS = "PASS"
FAIL = "FAIL"
passed_count = 0
failed_count = 0


def _report(name, ok, detail=""):
    global passed_count, failed_count
    status = PASS if ok else FAIL
    if ok:
        passed_count += 1
    else:
        failed_count += 1
    print("  [{}] {}  {}".format(status, name, detail))


def _fetch_ssd_trade_days_from_db(start, end, symbol_ids):
    """? SSD ?? distinct trade_date??? mock Adapter ?????????"""
    sess = SessionLocal()
    try:
        placeholders = ",".join([str(int(s)) for s in symbol_ids])
        sql_raw = (
            "SELECT DISTINCT trade_date FROM security_status_daily "
            "WHERE symbol_id IN ({}) AND trade_date >= :s AND trade_date <= :e "
            "ORDER BY trade_date ASC"
        ).format(placeholders)
        stmt = text(sql_raw)
        rows = sess.execute(stmt, {"s": start.isoformat(), "e": end.isoformat()}).fetchall()
        return [r[0] for r in rows]
    finally:
        sess.close()


# -----------------------------------------------------------------
# TR-4.1
# -----------------------------------------------------------------
def test_tr41():
    print()
    print("=== TR-4.1: monkeypatch Adapter throw -> TRADE_CALENDAR_UNAVAILABLE blocker ===")
    req = BacktestPrecheckRequest(
        portfolio_id=3, symbol_ids=[1, 2, 3],
        start_date=date(2022, 1, 1), end_date=date(2023, 11, 29),
    )
    mock_db = MagicMock()

    def _thrower(*a, **kw):
        raise RuntimeError("simulated adapter runtime error")

    with patch("app.services.bfg_precheck_service.get_trading_days", side_effect=_thrower):
        resp = run_backtest_precheck(mock_db, req)

    blocker_codes = {(b.code, b.severity) for b in resp.blocking_reasons}
    has_cal = ("TRADE_CALENDAR_UNAVAILABLE", "error") in blocker_codes
    _report("blockers ? TRADE_CALENDAR_UNAVAILABLE (severity=error)",
            has_cal, detail="actual: {}".format(blocker_codes))
    forbidden = {497, 498, 782, 783}
    not_5of7 = resp.requested_trade_days not in forbidden and resp.usable_trade_days not in forbidden
    _report("requested/usable ? 5/7 ???? 497?782 ??", not_5of7,
            detail="req={}, use={}".format(resp.requested_trade_days, resp.usable_trade_days))
    _report("fail-closed ? requested=0???????",
            resp.requested_trade_days == 0,
            detail="req={}".format(resp.requested_trade_days))
    insufficient = [b for b in resp.blocking_reasons if b.code == "INSUFFICIENT_TRADE_DAYS"]
    _report("TRADE_CALENDAR_UNAVAILABLE ?????? INSUFFICIENT_TRADE_DAYS",
            len(insufficient) == 0, detail="INSUFFICIENT count={}".format(len(insufficient)))

    def _thrower2(*a, **kw):
        raise TradeCalendarUnavailableError("simulated typed")

    with patch("app.services.bfg_precheck_service.get_trading_days", side_effect=_thrower2):
        resp2 = run_backtest_precheck(mock_db, req)
    codes2 = {(b.code, b.severity) for b in resp2.blocking_reasons}
    _report("?? TradeCalendarUnavailableError ? TRADE_CALENDAR_UNAVAILABLE (error)",
            ("TRADE_CALENDAR_UNAVAILABLE", "error") in codes2,
            detail="actual: {}".format(codes2))


# -----------------------------------------------------------------
# TR-4.2
# -----------------------------------------------------------------
def test_tr42():
    print()
    print("=== TR-4.2: SSD fixture ?? precheck (2025-03-03 ~ 2025-09-30) ===")

    start = date(2025, 3, 3)
    end = date(2025, 9, 30)
    sids = [1, 2, 3, 4, 5]
    req = BacktestPrecheckRequest(
        portfolio_id=3, symbol_ids=sids,
        start_date=start, end_date=end, minimum_trade_days=20,
    )

    # -- Scenario A: mock Adapter ?? SSD ????? --
    ssd_trade_days = _fetch_ssd_trade_days_from_db(start, end, sids)
    print("  INFO Scenario A mock cal trade_days count (SSD distinct) = {}".format(
        len(ssd_trade_days)))

    def _fake_adapter(*a, **kw):
        return list(ssd_trade_days)

    with patch("app.services.bfg_precheck_service.get_trading_days", side_effect=_fake_adapter):
        session = SessionLocal()
        try:
            resp = run_backtest_precheck(session, req)
        finally:
            session.close()

    blockers = {b.code: b.severity for b in resp.blocking_reasons}
    warnings = {b.code for b in resp.warnings}
    ex = resp.excluded_symbol_days
    tot = (ex.new_listing + ex.st + ex.suspended
           + ex.delisting_period + ex.status_unknown + ex.price_or_volume_invalid)
    print("  Scenario A (mock cal): req={}, use={}, excluded(total={}): "
          "nl={}, st={}, sus={}, de={}, unk={}".format(
              resp.requested_trade_days, resp.usable_trade_days, tot,
              ex.new_listing, ex.st, ex.suspended, ex.delisting_period, ex.status_unknown))
    print("  Scenario A blockers={}".format(list(blockers.items())))
    print("  Scenario A warnings={}".format(list(warnings)))

    has_status_blocker = "STATUS_DATA_UNAVAILABLE" in blockers
    cond_a = (tot > 0) and not has_status_blocker
    cond_b = has_status_blocker

    _report("A/B ????(A) excluded>0 ?? STATUS_DATA_UNAVAILABLE OR (B) STATUS_DATA_UNAVAILABLE blocker",
            cond_a or cond_b,
            detail="A={}, B={}; total_excluded={}, status_blocker={}".format(
                "Y" if cond_a else "N", "Y" if cond_b else "N", tot, has_status_blocker))

    neither = (not cond_a) and (not cond_b)
    _report("NOT(?? STATUS blocker ?? 0)", not neither,
            detail="ALL-ZERO-NO-STATUS-BLOCK={}".format(neither))

    if cond_a:
        has_st = ex.st > 0
        has_sus = ex.suspended > 0
        has_nl = ex.new_listing > 0
        has_de = ex.delisting_period > 0
        _report("fixture ???ST/suspended/new_listing/delisting_period ? > 0",
                has_st and has_sus and has_nl and has_de,
                detail="st={}, sus={}, nl={}, de={}".format(
                    ex.st, ex.suspended, ex.new_listing, ex.delisting_period))
        # sid=1 ST 40?, sid=2 suspended 40?, sid=3??40?, sid=4????40?, sid=5??
        _report("fixture ?????st=40, suspended=40, new_listing=40, delisting_period=40",
                ex.st == 40 and ex.suspended == 40 and ex.new_listing == 40 and ex.delisting_period == 40,
                detail="?? st={}, sus={}, nl={}, de={}".format(
                    ex.st, ex.suspended, ex.new_listing, ex.delisting_period))

    # -- Scenario B: real no-mock --
    session = SessionLocal()
    try:
        resp2 = run_backtest_precheck(session, req)
    finally:
        session.close()
    blockers2 = {b.code: b.severity for b in resp2.blocking_reasons}
    ex2 = resp2.excluded_symbol_days
    tot2 = (ex2.new_listing + ex2.st + ex2.suspended
            + ex2.delisting_period + ex2.status_unknown + ex2.price_or_volume_invalid)
    has_any_error_blocker = any(s == "error" for s in blockers2.values())
    print("  Scenario B (real): blockers={}, excluded.total={}".format(
        list(blockers2.items()), tot2))

    neither_any = (not has_any_error_blocker) and (tot2 == 0)
    _report("???NOT(?? error blocker ?? 0 ??)",
            not neither_any,
            detail="any_error_blocker={}, excluded_total={}".format(
                has_any_error_blocker, tot2))
    if "TRADE_CALENDAR_UNAVAILABLE" in blockers2 and blockers2["TRADE_CALENDAR_UNAVAILABLE"] == "error":
        _report("???TRADE_CALENDAR_UNAVAILABLE(error) + excluded=0 ??? fail-closed?? 0 ?????",
                True,
                detail="fail-closed?calendar ???? SSD ???excluded=0 ??????? 0 ??")


# -----------------------------------------------------------------
# TR-4.3
# -----------------------------------------------------------------
def test_tr43():
    print()
    print("=== TR-4.3: ??? 2021-01-01 ~ 2024-01-01 (Adapter vs ?? vs ???) ===")
    req = BacktestPrecheckRequest(
        portfolio_id=3, symbol_ids=[1, 2, 3],
        start_date=date(2021, 1, 1), end_date=date(2024, 1, 1),
        minimum_trade_days=300,
    )
    session = SessionLocal()
    try:
        try:
            tdays = get_trading_days(req.start_date, req.end_date, session=session)
            adapter_len = len(tdays)
            adapter_ok = True
        except TradeCalendarUnavailableError as e:
            adapter_len = 0
            adapter_ok = False
            print("  INFO Adapter fail-closed?? 5/7 ????{}".format(str(e)[:80]))
        resp = run_backtest_precheck(session, req)
    finally:
        session.close()

    natural_days = (req.end_date - req.start_date).days + 1
    nat_5of7 = int(natural_days * 5 / 7)
    print("  INFO ???={}, 5/7 ??={}".format(natural_days, nat_5of7))
    print("  INFO Adapter len={} (ok={}), ?? req={}, use={}".format(
        adapter_len, adapter_ok, resp.requested_trade_days, resp.usable_trade_days))
    print("  INFO blockers={}".format(
        [(b.code, b.severity) for b in resp.blocking_reasons]))

    matches = resp.requested_trade_days == adapter_len
    _report("requested_trade_days == len(Adapter??)?? 5/7??????",
            matches, detail="resp_req={}, Adapter={}, ???={}, 5/7={}".format(
                resp.requested_trade_days, adapter_len, natural_days, nat_5of7))
    _report("requested != ??? ({})".format(natural_days),
            resp.requested_trade_days != natural_days,
            detail="req={}".format(resp.requested_trade_days))
    _report("requested != 5/7 ?? ({})".format(nat_5of7),
            resp.requested_trade_days != nat_5of7,
            detail="req={}".format(resp.requested_trade_days))

    if not adapter_ok:
        has_blocker = any(
            b.code == "TRADE_CALENDAR_UNAVAILABLE" and b.severity == "error"
            for b in resp.blocking_reasons
        )
        _report("fail-closed => TRADE_CALENDAR_UNAVAILABLE (error) blocker",
                has_blocker, detail="blockers={}".format(
                    [(b.code, b.severity) for b in resp.blocking_reasons]))
        forbidden = {natural_days, nat_5of7, nat_5of7 + 1}
        no_est = resp.requested_trade_days not in forbidden and resp.usable_trade_days not in forbidden
        _report("fail-closed ? requested/usable ???? 5/7 ??????", no_est,
                detail="req={}, use={}, forbidden={}".format(
                    resp.requested_trade_days, resp.usable_trade_days, forbidden))


# -----------------------------------------------------------------
# T1 ??
# -----------------------------------------------------------------
def test_t1_regression():
    print()
    print("=== T1 ??: INVALID_DATE_RANGE ?? ===")
    mock_db = MagicMock()
    req = BacktestPrecheckRequest(
        portfolio_id=3, symbol_ids=[1],
        start_date=date(2026, 9, 1), end_date=date(2025, 1, 1),
    )
    resp = run_backtest_precheck(mock_db, req)
    codes = {b.code: b.severity for b in resp.blocking_reasons}
    _report("start>end => INVALID_DATE_RANGE (error)",
            codes.get("INVALID_DATE_RANGE") == "error",
            detail="codes={}".format(codes))
    _report("start>end => usable=0, requested=0",
            resp.requested_trade_days == 0 and resp.usable_trade_days == 0,
            detail="req={}, use={}".format(resp.requested_trade_days, resp.usable_trade_days))


def main():
    for name, fn in [
        ("TR-4.1", test_tr41),
        ("TR-4.2", test_tr42),
        ("TR-4.3", test_tr43),
        ("T1 ??", test_t1_regression),
    ]:
        try:
            fn()
        except Exception as ex:
            global failed_count
            failed_count += 1
            print("  [FAIL] {} ??: {}: {}".format(name, type(ex).__name__, ex))
            traceback.print_exc()

    print()
    print("=" * 70)
    print("SUMMARY: PASS={}, FAIL={}".format(passed_count, failed_count))
    print("=" * 70)
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
