"""为 CI 的 e2e 闸门预置"能让页面渲染出持仓成员行"的最小数据。

为什么需要这一步（实测，不是猜测）：
e2e 是零桩真实链路（浏览器 + 真后端 + 真库）。CI 的库由后端启动时的 init_db() 新建，
实测内容：symbols=0 / portfolios=0 / portfolio_members=0（对照本地开发库 gpfx：
7639 / 12 / 6）。于是「组合交易 → 持仓成员」表只能显示空态占位行，
"行内应有 再平衡/清仓 入口"那条用例必然红 —— 它在赌"环境里已经有数据"。
而 `symbol_id` 是外键、CI 又拉不到 akshare 行情底座，所以用例**不可能**自己
通过 UI 造出成员；把"环境具备可渲染数据"做成环境里的显式一步，就是本脚本。

安全设计（沿用 scripts/seed_ui_acceptance.py 已确立的规矩，只换白名单形式）：
1. **用 ORM 模型造对象**，不写裸 SQL 列名清单 —— 模型加/改字段时种子自动跟随，
   真缺必填字段会在种的那一刻抛错（CI 步骤直接红并指出缺什么），
   而不是变成"种子上次能跑、这次静默种错"的第二套真相。
2. **双门禁**（任一不满足 → 返回码 2 且不写一行数据）：
   - 只允许在 CI 运行（环境变量 CI=true/1）；
   - 目标库必须是 CI 专用库名白名单内（默认 gpfx_ci）且必须是 MySQL。
   白名单独立于连接凭据：即使有人把 db_config.json 指向真实开发库/生产库，也拒绝。
3. **按业务键先查后插**，重复执行幂等；**绝不做 DELETE/TRUNCATE**，不覆盖已有数据。
4. 表不存在时直接拒绝并提示"必须在后端建库之后运行"，防止步骤顺序被改坏后静默空种。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 只允许写这几个库名。新增 CI 数据库时改这里，并在 PR 里说明理由。
CI_DB_ALLOWLIST = frozenset({"gpfx_ci"})

SEED_PORTFOLIO_NAME = "CI-E2E-种子组合"
# (代码, 名称, 交易所, 现价)
SEED_SYMBOLS = (
    ("600997", "CI 种子标的甲", "SSE", 12.50),
    ("000997", "CI 种子标的乙", "SZSE", 9.80),
)
_REQUIRED_TABLES = frozenset({"symbols", "portfolios", "portfolio_members", "positions"})


def gate_error(ci_flag: str | None, use_mysql: bool, db_name: str) -> str | None:
    """返回 None 表示放行；否则返回拒绝原因。（纯函数，便于本地把三条分支都测到）"""
    if str(ci_flag or "").lower() not in {"true", "1"}:
        return (
            "只允许在 CI 运行（需要 CI=true/1）。"
            "本地开发库不该被 CI 种子改写 —— 想在本机看演示数据请另写脚本，别放开这道门禁。"
        )
    if not use_mysql:
        return "当前数据库配置不是 MySQL；CI 的 e2e 跑在 MySQL 服务上（与生产同方言）。"
    if db_name not in CI_DB_ALLOWLIST:
        return (
            f"目标库 {db_name!r} 不在 CI 库名白名单 {sorted(CI_DB_ALLOWLIST)} 内，"
            "拒绝写入（防止把种子指向开发库/生产库）。"
        )
    return None


def seed(db) -> dict[str, int]:
    """幂等写入最小数据。返回各表本次新增/已存在的计数。"""
    from sqlalchemy import select

    from app.models.portfolio import Portfolio, Position
    from app.models.portfolio_member import PortfolioMember
    from app.models.symbol import Symbol

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stats = {"symbols": 0, "portfolios": 0, "members": 0, "positions": 0, "reused": 0}

    pf = db.execute(
        select(Portfolio).where(Portfolio.name == SEED_PORTFOLIO_NAME)
    ).scalars().first()
    if pf is None:
        pf = Portfolio(
            name=SEED_PORTFOLIO_NAME,
            account_type="simulated",
            total_capital=200000.0,
            investable_ratio=0.9,
            cash_reserve_ratio=0.1,
            currency="CNY",
            is_default=0,
            auto_trade_enabled=1,
        )
        db.add(pf)
        db.flush()
        stats["portfolios"] += 1
    else:
        stats["reused"] += 1

    for code, name, market, price in SEED_SYMBOLS:
        sym = db.execute(select(Symbol).where(Symbol.symbol == code)).scalars().first()
        if sym is None:
            sym = Symbol(symbol=code, name=name, asset_type="stock", market=market, board="main")
            db.add(sym)
            db.flush()
            stats["symbols"] += 1
        else:
            stats["reused"] += 1

        # 成员：页面与预检都按"当前有效成员"取（status=active, effective_to IS NULL）
        member = db.execute(
            select(PortfolioMember).where(
                PortfolioMember.portfolio_id == pf.id,
                PortfolioMember.symbol_id == sym.id,
                PortfolioMember.effective_to.is_(None),
            )
        ).scalars().first()
        if member is None:
            db.add(
                PortfolioMember(
                    portfolio_id=pf.id, symbol_id=sym.id,
                    status="active", effective_from=now, effective_to=None,
                    source_type="manual", note="CI e2e 种子",
                )
            )
            stats["members"] += 1
        else:
            stats["reused"] += 1

        # 持仓行才是这张表的数据源（GET /portfolios/{id}/positions 直接查 positions 表）
        pos = db.execute(
            select(Position).where(
                Position.portfolio_id == pf.id, Position.symbol_id == sym.id
            )
        ).scalars().first()
        if pos is None:
            db.add(
                Position(
                    portfolio_id=pf.id, symbol_id=sym.id,
                    quantity=1000.0, avg_cost=price, latest_price=price,
                    market_value=price * 1000.0, position_pct=price * 1000.0 / 200000.0,
                    asset_type="stock", opened_at=now, updated_at=now,
                )
            )
            stats["positions"] += 1
        else:
            stats["reused"] += 1

    db.commit()
    return stats


def main() -> int:
    from app.core.config import build_mysql_url, load_db_config

    cfg = load_db_config()
    mysql = cfg.get("mysql") or {}
    reason = gate_error(
        os.environ.get("CI"), bool(cfg.get("use_mysql")), str(mysql.get("database") or "")
    )
    if reason:
        print(f"ci_seed_e2e_fixture 拒绝执行：{reason}", file=sys.stderr)
        return 2

    from sqlalchemy import inspect
    from app.db.manager import DatabaseManager
    from app.db.session import get_engine, get_session_local

    DatabaseManager.get().initialize(build_mysql_url(cfg), db_type="mysql")
    names = set(inspect(get_engine()).get_table_names())
    missing = _REQUIRED_TABLES - names
    if missing:
        print(
            f"ci_seed_e2e_fixture 拒绝执行：库里还缺这些表 {sorted(missing)}；"
            "种子必须排在后端 init_db() 建库之后运行（检查 workflow 步骤顺序）。",
            file=sys.stderr,
        )
        return 2

    SessionLocal = get_session_local()
    with SessionLocal() as db:
        stats = seed(db)
    print(
        f"CI e2e 种子完成（库 {mysql.get('database')}）："
        f"新增 symbols={stats['symbols']} portfolios={stats['portfolios']} "
        f"members={stats['members']} positions={stats['positions']}，"
        f"复用已存在记录={stats['reused']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
