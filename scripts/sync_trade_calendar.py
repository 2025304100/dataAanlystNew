#!/usr/bin/env python
"""用权威交易日历校正/同步 `trade_calendar` 表。

用法：
    python scripts/sync_trade_calendar.py            # 只读对比，不写库
    python scripts/sync_trade_calendar.py --apply    # 对比 + 备份 + 写库

为什么需要这个脚本
==================
`trade_calendar` 原先的内容等价于 `is_trading_day = (weekday < 5)` —— 只有周末规则、
没有节假日规则。实测各年交易日数为 260/262/261/261，而真实 A 股是 242~244，
每年多算 18~20 天。后果是 `bfg_precheck_service`（回测预检）的「可用交易日数」
虚高。2026-10-01 校正过一次（75 行），本脚本是该校正的固化入口。

数据来源
========
`akshare.tool_trade_date_hist_sina()`，底层是新浪财经的
`https://finance.sina.com.cn/realstock/company/klc_td_sh.txt`（文件名里的 sh 指上交所）。
该接口返回 1990-12-19 起的完整交易日列表，**含未来约一年的安排** ——
国务院每年 11 月公布下一年放假安排后，新浪同步更新，所以不需要自己"预测"。

维护节奏
========
每年 11 月国务院发布下一年安排后跑一次 `--apply` 即可，平时无需操作。

安全设计
========
- 默认 dry-run，必须显式 `--apply` 才写库；
- 写库前做合理性校验（关键节假日 + 年度交易日数），任一项不过直接终止；
- 写库前把现有全表导出为 JSON 备份到 `.workbuddy/mining/`；
- 只更新表内**已存在**的日期，不扩表范围（扩范围是独立决策）；
- 全部更新在同一事务内完成。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402

BACKUP_DIR = ROOT / ".workbuddy" / "mining"

# 关键日校验：(日期, 期望是否交易日, 说明)
_SANITY_ANCHORS: list[tuple[str, bool, str]] = [
    ("2025-10-01", False, "国庆"),
    ("2025-10-09", True, "国庆后首个交易日"),
    ("2026-10-01", False, "国庆"),
    ("2026-09-30", True, "节前最后交易日"),
]


def fetch_authoritative_days() -> set[str]:
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    return {str(d) for d in df["trade_date"].astype(str)}


def sanity_check(days: set[str]) -> bool:
    ok = True
    for iso, expect_trading, label in _SANITY_ANCHORS:
        hit = iso in days
        passed = hit == expect_trading
        ok = ok and passed
        print(f"    {'PASS' if passed else 'FAIL'}  {iso} {label} 应{'开市' if expect_trading else '休市'}")
    # 年度交易日数应在 235~250 之间（真实 A 股 242~244）
    from collections import Counter

    per_year = Counter(d[:4] for d in days)
    for year in ("2023", "2024", "2025", "2026"):
        n = per_year.get(year, 0)
        passed = 235 <= n <= 250
        ok = ok and passed
        print(f"    {'PASS' if passed else 'FAIL'}  {year} 年交易日数 = {n}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写库（缺省只做只读对比）")
    args = parser.parse_args()

    print("[1] 拉取权威交易日历 ...")
    truth = fetch_authoritative_days()
    print(f"    {len(truth)} 天，范围 {min(truth)} ~ {max(truth)}")

    print("[2] 合理性校验 ...")
    if not sanity_check(truth):
        print("!! 校验未通过，终止（不写库）")
        return 1

    engine = create_engine(build_mysql_url(load_db_config()), future=True)

    print("[3] 与库内 trade_calendar 对比 ...")
    with engine.connect() as conn:
        rows = list(conn.execute(
            text("SELECT date, is_trading_day FROM trade_calendar ORDER BY date")
        ).mappings())
    if not rows:
        print("    表为空，无可对比范围（本脚本不负责初始化表结构）")
        return 0
    print(f"    表内 {len(rows)} 行，范围 {rows[0]['date']} ~ {rows[-1]['date']}")

    to_open, to_close = [], []
    for row in rows:
        current = int(row["is_trading_day"])
        should = 1 if str(row["date"]) in truth else 0
        if current != should:
            (to_open if should == 1 else to_close).append(str(row["date"]))

    print(f"    需修改 {len(to_open) + len(to_close)} 行"
          f"（应开市却标休市 {len(to_open)} / 应休市却标开市 {len(to_close)}）")
    if to_close:
        print(f"      应休市却标开市：{to_close[:20]}{' ...' if len(to_close) > 20 else ''}")
    if to_open:
        print(f"      应开市却标休市：{to_open[:20]}{' ...' if len(to_open) > 20 else ''}")

    if not to_open and not to_close:
        print("已一致，无需修改。")
        return 0

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 重新执行。")
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = BACKUP_DIR / f"trade_calendar_backup_{stamp}.json"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(
        [{"date": str(r["date"]), "is_trading_day": int(r["is_trading_day"])} for r in rows],
        ensure_ascii=False,
    ), encoding="utf-8")
    print(f"[4] 已备份 {len(rows)} 行 -> {backup}")

    print("[5] 写入（单事务）...")
    changed = 0
    with engine.begin() as conn:
        for row in rows:
            current = int(row["is_trading_day"])
            should = 1 if str(row["date"]) in truth else 0
            if current != should:
                conn.execute(
                    text("UPDATE trade_calendar SET is_trading_day = :v WHERE date = :d"),
                    {"v": should, "d": row["date"]},
                )
                changed += 1
    print(f"    更新 {changed} 行")

    print("[6] 验证 ...")
    with engine.connect() as conn:
        for row in conn.execute(text(
            "SELECT YEAR(date) y, SUM(is_trading_day=1) n FROM trade_calendar "
            "GROUP BY YEAR(date) ORDER BY y"
        )).mappings():
            print(f"    {row['y']}: {int(row['n'])} 个交易日")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
