"""白盒 - CI e2e 种子的门禁、幂等性与跨文件一致性。

为什么这些断言值得进闸门（而不是留在一次性验证脚本里）：
种子的真正风险不是"种错一行"，而是它**长成第二套真相**：
- 有人把 workflow 里的 CI 库名改了，白名单还留着旧名 → 种子静默拒绝，e2e 又空库红；
- 有人把种子挂到 app/ 启动路径上 → CI 的造数工具变成生产副作用；
- 有人重跑种子 → 如果没做幂等，CI 每次多出几行持仓，页面数字越来越离谱。
这四条各钉一个。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import PortfolioMember
from scripts import ci_seed_e2e_fixture as seeder

pytestmark = pytest.mark.whitebox

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. 双门禁
# ---------------------------------------------------------------------------


def test_gate_refuses_when_not_ci():
    assert seeder.gate_error(None, True, "gpfx_ci"), "非 CI 必须拒绝"
    assert seeder.gate_error("false", True, "gpfx_ci"), "CI=false 也必须拒绝"


def test_gate_refuses_non_mysql():
    assert seeder.gate_error("true", False, "gpfx_ci"), "非 MySQL 必须拒绝（CI e2e 跑 MySQL）"


def test_gate_refuses_development_database_even_in_ci():
    """最关键的一条：冒充 CI 也写不到开发库/生产库。"""
    for name in ("gpfx", "gpfx_prod", "mysql", ""):
        reason = seeder.gate_error("true", True, name)
        assert reason, f"库名 {name!r} 不在白名单却被放行"
        assert "白名单" in reason


def test_gate_allows_ci_database():
    assert seeder.gate_error("true", True, "gpfx_ci") is None
    assert seeder.gate_error("1", True, "gpfx_ci") is None


# ---------------------------------------------------------------------------
# 2. 幂等 + 接口口径可见
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_seed_creates_rows_and_is_idempotent(db):
    first = seeder.seed(db)
    assert first["portfolios"] == 1
    assert first["symbols"] == len(seeder.SEED_SYMBOLS)
    assert first["members"] == len(seeder.SEED_SYMBOLS)
    assert first["positions"] == len(seeder.SEED_SYMBOLS)

    second = seeder.seed(db)
    assert second["portfolios"] == 0 and second["symbols"] == 0, (
        f"重跑必须只复用不新增，实际第二次={second}（否则 CI 每次多出持仓）"
    )
    assert second["members"] == 0 and second["positions"] == 0
    assert second["reused"] > 0


def test_seeded_rows_are_visible_to_the_endpoints_the_ui_uses(db):
    """成员表那页真正读的是 positions；这里按接口同款条件查回来。

    - GET /api/v1/portfolios/{id}/positions → select(Position).where(portfolio_id)
    - 「当前有效成员」口径 → status=active 且 effective_to IS NULL（预检 auto-fill 同源）
    """
    seeder.seed(db)

    pf = db.execute(
        select(Portfolio).where(Portfolio.name == seeder.SEED_PORTFOLIO_NAME)
    ).scalars().one()
    positions = db.execute(
        select(Position).where(Position.portfolio_id == pf.id)
    ).scalars().all()
    members = db.execute(
        select(PortfolioMember).where(
            PortfolioMember.portfolio_id == pf.id,
            PortfolioMember.status == "active",
            PortfolioMember.effective_to.is_(None),
        )
    ).scalars().all()

    assert len(positions) == len(seeder.SEED_SYMBOLS), (
        "持仓行数不足：e2e 的成员表还是会渲染成空态占位行"
    )
    assert len(members) == len(seeder.SEED_SYMBOLS)
    # 页面靠 symbol/name 认行；缺关联标的就会显示成"代码未解析"
    ids = {p.symbol_id for p in positions}
    assert db.execute(select(Portfolio).where(Portfolio.id == pf.id)).first() is not None
    assert ids and all(i for i in ids)


# ---------------------------------------------------------------------------
# 3. 跨文件一致性（防"第二套真相"）
# ---------------------------------------------------------------------------


def test_workflow_ci_db_name_is_in_seed_allowlist():
    """workflow 里写的 CI_DB_NAME 必须在种子白名单里。

    两边不一致时的故障很难看懂：种子拒绝执行 → e2e 空库 → 报"没有成员行"，
    看起来像 UI 回归。这条断言把它变成一条明确失败。
    """
    text = (REPO / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    names = set(re.findall(r"CI_DB_NAME:\s*\"?([A-Za-z0-9_]+)\"?", text))
    assert names, "workflow 里找不到 CI_DB_NAME，步骤被改了请同步本测试"
    unknown = names - set(seeder.CI_DB_ALLOWLIST)
    assert not unknown, (
        f"workflow 用的 CI 库名 {sorted(unknown)} 不在 ci_seed_e2e_fixture.CI_DB_ALLOWLIST；"
        "要么同步白名单，要么改回库名 —— 否则 e2e 种子会静默不执行"
    )
    # services.mysql 的 MYSQL_DATABASE 也要同一个库
    for m in re.finditer(r"MYSQL_DATABASE:\s*([A-Za-z0-9_]+)", text):
        assert m.group(1) in seeder.CI_DB_ALLOWLIST, (
            f"services 建的库 {m.group(1)} 与种子白名单不一致"
        )


def test_app_code_never_references_the_ci_seed_script():
    """种子是 CI 的造数工具，绝不能被应用启动路径引用。

    可证伪：在 app/ 里加一行 `import scripts.ci_seed_e2e_fixture` 就会变红。
    """
    offenders = []
    for path in (REPO / "app").rglob("*.py"):
        try:
            body = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        if "ci_seed_e2e_fixture" in body:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        f"应用代码引用了 CI 种子脚本（会让造数工具变成生产副作用）：{offenders}"
    )
