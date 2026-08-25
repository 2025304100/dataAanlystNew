"""T-B1 Q2.1：撮合策略统一 NEXT_OPEN 默认 + 禁止 T 日 close 全局常量。

覆盖：
- T-B1.1：正式链路（production / strict_pit / production_pit / production_sim）+ match_mode=T_CLOSE
  → ValueError meta.error_code=USE_OF_T_CLOSE_VIOLATION
- T-B1.2：研究模式 run_mode=research + match_mode=T_CLOSE + **显式** allow_t_close_research_override=True
  → resolved=T_CLOSE，degraded_warnings 含 RESEARCH_ONLY_T_CLOSE，is_result_production_eligible=False
- T-B1.3（额外）：研究模式 T_CLOSE 但未显式 override → RESEARCH_T_CLOSE_REQUIRES_EXPLICIT_OVERRIDE
- B0：None → 默认 NEXT_OPEN，degraded_warnings=[]，eligible=True（零回退控制组）
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """与 G1 系列一致：迁移空 SQLite（T-B1 本身是纯函数，不依赖 DB；保持 fixture 约定以便扩展集成测）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_tb1_match_mode_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


class TestTB1MatchModeValidation:
    # ────────────────────────────────────────────────────── T-B1.1: 正式阻断
    @pytest.mark.parametrize("run_mode", ["production", "strict_pit", "production_pit", "production_sim"])
    def test_t_b1_1_prod_t_close_raises_use_of_t_close_violation(self, run_mode):
        """T-B1.1: 所有正式链路 run_mode 使用 T_CLOSE → ValueError(meta.error_code=USE_OF_T_CLOSE_VIOLATION)。"""
        from app.services.decision_clock import validate_match_mode, MatchMode

        with pytest.raises(ValueError) as exc_info:
            validate_match_mode(
                mode=MatchMode.T_CLOSE,
                run_mode=run_mode,
                allow_t_close_research_override=False,
            )

        err = exc_info.value
        meta = err.__dict__.get("meta") or getattr(err, "meta", None)
        assert meta is not None, f"expected meta on err, got attrs={list(err.__dict__.keys())}"
        assert meta["error_code"] == "USE_OF_T_CLOSE_VIOLATION"
        assert "T_CLOSE" in str(err)

    # ────────────────────────────────────────────────────── T-B1.2: research override 允许但降级
    def test_t_b1_2_research_t_close_explicit_override_yields_degraded_eligible_false(self):
        """T-B1.2: research + T_CLOSE + allow_t_close_research_override=True
        → resolved=T_CLOSE, warnings 有 RESEARCH_ONLY_T_CLOSE, eligible=False。
        """
        from app.services.decision_clock import validate_match_mode, MatchMode

        resolved, warnings, eligible = validate_match_mode(
            mode="T_CLOSE",  # 故意用字符串，验证 str 兼容路径
            run_mode="research",
            allow_t_close_research_override=True,
        )
        assert resolved == MatchMode.T_CLOSE
        assert eligible is False
        assert any(w.get("code") == "RESEARCH_ONLY_T_CLOSE" for w in warnings)
        msg_entry = [w for w in warnings if w.get("code") == "RESEARCH_ONLY_T_CLOSE"][0]
        assert msg_entry.get("detail", {}).get("run_mode") == "research"

    # ────────────────────────────────────────────────────── T-B1.2 ext: research 没显式 override → 422 阻断
    def test_t_b1_2c_research_t_close_without_explicit_override_raises_requires_override(self):
        """额外：研究模式 T_CLOSE 但 allow_t_close_research_override 未显式 True → RESEARCH_T_CLOSE_REQUIRES_EXPLICIT_OVERRIDE。"""
        from app.services.decision_clock import validate_match_mode, MatchMode

        with pytest.raises(ValueError) as exc_info:
            validate_match_mode(mode=MatchMode.T_CLOSE, run_mode="research", allow_t_close_research_override=None)

        meta = exc_info.value.__dict__.get("meta") or getattr(exc_info.value, "meta", None)
        assert meta is not None
        assert meta["error_code"] == "RESEARCH_T_CLOSE_REQUIRES_EXPLICIT_OVERRIDE"

    # ────────────────────────────────────────────────────── B0 控制组：None 默认 NEXT_OPEN
    @pytest.mark.parametrize("run_mode", ["production", "research", "strict_pit"])
    def test_t_b1_b0_default_none_is_next_open_zero_regression(self, run_mode):
        """B0: match_mode=None（不传）→ 默认 NEXT_OPEN，不阻断、不降级（向后兼容）。"""
        from app.services.decision_clock import validate_match_mode, MatchMode, DEFAULT_MATCH_MODE

        resolved, warnings, eligible = validate_match_mode(mode=None, run_mode=run_mode, allow_t_close_research_override=None)
        assert resolved == DEFAULT_MATCH_MODE == MatchMode.NEXT_OPEN
        assert warnings == []
        assert eligible is True

    # ────────────────────────────────────────────────────── 额外：字符串大小写宽松解析（"next_open"→OK）
    def test_t_b1_ext_case_insensitive_string_resolves_next_open(self):
        from app.services.decision_clock import validate_match_mode, MatchMode

        resolved, warnings, eligible = validate_match_mode(mode="next_open", run_mode="production")
        assert resolved == MatchMode.NEXT_OPEN
        assert warnings == []
        assert eligible is True
