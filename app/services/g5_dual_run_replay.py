"""G5 双跑加速回放服务（AC-15 新旧决策链 20 交易日逐证券对比）。

对齐 spec AC-15 口径：
  - Given: G4 黑盒通过；新旧决策链并行（chain A = 旧 scan 来源；chain B = PortfolioCandidate
    成员来源 + PIT 安全 + 三硬门禁），**不产生订单**（仅写入双跑快照）。
  - When: 连续 20 个交易日（默认）双跑加速回放（一次性跑完所有 trade_date，无需等待自然日）。
  - Then: 逐日逐证券比较「候选 / 动作 / 仓位 / 拒绝原因」；差异 100% 有归因分类。
  - P0 验收: 20 日 P0/P1 未解释差异 = 0 → 方可进入 G6 灰度。

G5 与 WP7.3 compare_new_old_engine 的区别：
  - 后者是「回测级」全量指标对比；本服务是「决策级」逐日逐证券结构化对比，
    粒度对齐 AC-15 的候选/动作/拒绝原因。

对比维度（每个 trade_date 生成 DailyDualRunReport）：
  1. universe: 候选集 Jaccard、新增、移除（按 symbol_id）
  2. actions: BUY/SELL/HOLD/NO_ACTION/REJECTED/DATA_BLOCKED 六类 6x6 混淆矩阵
  3. reasons: 拒绝原因 top_k 分布（new_old / old_new / both_same_reject_diff_reason）
  4. positions: 目标仓位（若可计算）Pearson + 偏差 > threshold 的证券列表

未解释差异清零规则（G5 pass → G6 前置）：
  - P0: BUY/SELL 动作不一致且 reason_category ∈ {UNKNOWN_ENGINE_DIFF, UNMAPPED} 的 count = 0
  - P1: HOLD/NO_ACTION 方向相反的 count <= 0（即 0）的交易日比例 = 100%
  - 若未达标：返回 failing_days，便于逐天归因。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

ACTION_TYPES = ("BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED")

ReasonCategory = Literal[
    # 可解释分类
    "MEMBER_SOURCE_DIFF",          # 旧/新标的来源不一致导致（候选池不同）
    "PIT_CUTOFF_DIFF",             # data_cutoff_at 时点不一致（20:00 vs 15:05）
    "SCORE_DATA_GAP",              # Score 存在性/分位差异
    "HARD_GATE_INTERVENTION",      # 三硬门禁（HG1/HG2/HG3）阻断导致
    "COVERAGE_THRESHOLD_DIFF",     # Score 覆盖率 Production=95% vs Research=90% 门槛差异
    "RISK_CHECK_BLOCK",            # 组合风险检查（最大仓位/持仓数）不同步
    "EXPECTED_STRUCTURAL_DIFF",    # 预期结构差异（如新引擎明确弃用旧 heuristic）
    # P0 阻断分类（必须清零才能进 G6）
    "UNKNOWN_ENGINE_DIFF",         # 未解释：引擎行为差异，需人工归因
    "UNMAPPED",                    # 未映射：未归类原因
]


# ============================================================================
# 数据结构（对齐 AC-15 六类动作 + 拒绝原因）
# ============================================================================
@dataclass
class SecurityDecision:
    """单证券单决策日的结构化决策快照。"""
    symbol_id: int
    symbol: str | None = None
    action: str = "NO_ACTION"            # BUY/SELL/HOLD/NO_ACTION/REJECTED/DATA_BLOCKED
    target_weight: float | None = None   # 目标仓位权重，0..1
    score_value: float | None = None
    reject_reason_code: str | None = None      # 机读原因码，如 "DATA_BLOCKED__SCORE_MISSING"
    reject_reason_human: str | None = None     # 人读原因
    reason_category: ReasonCategory = "UNMAPPED"
    checks_json: dict[str, Any] | None = None  # 决策检查项

    def to_audit_row(self) -> dict:
        return {
            "symbol_id": self.symbol_id,
            "action": self.action,
            "target_weight": self.target_weight,
            "reject_code": self.reject_reason_code,
            "reason_category": self.reason_category,
        }


@dataclass
class DailyChainResult:
    """单链（A 或 B）在某 trade_date 的决策结果快照。"""
    chain: Literal["A", "B"]               # A=旧来源，B=新来源
    trade_date: date
    universe_symbol_ids: list[int] = field(default_factory=list)
    decisions: dict[int, SecurityDecision] = field(default_factory=dict)  # symbol_id -> Decision
    coverage_pct: float = 0.0              # Score 覆盖率 %
    blocking_status: str = "READY"         # READY / BLOCKED
    blocking_reasons: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DailyDualRunReport:
    """单日双跑对比报告（AC-15 粒度）。"""
    trade_date: date
    portfolio_id: int = 0
    chain_a_id: str = ""
    chain_b_id: str = ""
    # 每日来源元数据必须与报告一并归档，G6 准入据此拒绝 synthetic/test 回放。
    chain_a_meta: dict[str, Any] = field(default_factory=dict)
    chain_b_meta: dict[str, Any] = field(default_factory=dict)
    # 1) universe
    universe_jaccard: float = 0.0
    universe_added: list[int] = field(default_factory=list)   # B \ A（新来源独有）
    universe_removed: list[int] = field(default_factory=list)  # A \ B（旧来源独有）
    # 2) actions 混淆矩阵: confusion[action_a][action_b] = count
    action_confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    # 3) 动作不一致并按 reason_category 聚合（P0/P1 清零判定用）
    inconsistent_by_category: dict[str, int] = field(default_factory=dict)
    # 4) 单证券明细差异（最多 top_k，避免超大 JSON）
    symbol_diffs: list[dict[str, Any]] = field(default_factory=list)
    # 指标
    action_match_rate: float = 0.0   # (动作完全一致的证券数) / (A ∪ B 的证券数)
    p0_unexplained_count: int = 0    # BUY/SELL 方向相反 + UNKNOWN/UNMAPPED
    p1_hold_noaction_flip: int = 0   # HOLD↔NO_ACTION 反转
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["trade_date"] = self.trade_date.isoformat()
        return d


@dataclass
class G5ReplaySummary:
    """20 日双跑加速回放总报告（进 G6 的准入判断依据）。"""
    portfolio_id: int
    start_date: date
    end_date: date
    total_days: int = 0
    days_replayed: int = 0
    skipped_days: list[date] = field(default_factory=list)
    daily_reports: list[DailyDualRunReport] = field(default_factory=list)
    # 汇总
    avg_action_match_rate: float = 0.0
    avg_universe_jaccard: float = 0.0
    total_p0_unexplained: int = 0
    total_p1_hold_noaction_flip: int = 0
    failing_days_p0: list[date] = field(default_factory=list)
    failing_days_p1: list[date] = field(default_factory=list)
    g5_eligible_for_g6: bool = False   # P0+P1 均通过 → True
    summary_notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start_date"] = self.start_date.isoformat()
        d["end_date"] = self.end_date.isoformat()
        d["skipped_days"] = [d.isoformat() for d in self.skipped_days]
        d["failing_days_p0"] = [d.isoformat() for d in self.failing_days_p0]
        d["failing_days_p1"] = [d.isoformat() for d in self.failing_days_p1]
        d["daily_reports"] = [r.as_dict() for r in self.daily_reports]
        return d


# ============================================================================
# 交易日历：生成 N 个连续工作日（简单近似，真实接入可用 trade_calendar.py）
# ============================================================================
def generate_20_trade_days_backward(anchor: date, *, n_days: int = 20) -> list[date]:
    """从 anchor 向前生成 n_days 个连续交易日（跳过周六/周日）。

    真实生产可替换为 HKEX/SSE trade_calendar 查询，这里用 weekday() 近似：
      weekday()=5/6 → Sat/Sun，跳过。
    """
    days: list[date] = []
    cur = anchor
    while len(days) < n_days:
        wd = cur.weekday()
        if wd < 5:  # Mon-Fri
            days.append(cur)
        cur -= timedelta(days=1)
    # 正向顺序
    return list(reversed(days))


def _chain_id(chain: Literal["A", "B"], trade_date: date, portfolio_id: int,
              seed: str = "") -> str:
    raw = f"g5|{chain}|P{portfolio_id}|{trade_date.isoformat()}|{seed}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ============================================================================
# 单天双跑对比主逻辑（对齐 AC-15 六动作 + 原因分类）
# ============================================================================
def build_confusion_matrix_6x6() -> dict[str, dict[str, int]]:
    return {a: {b: 0 for b in ACTION_TYPES} for a in ACTION_TYPES}


def classify_diff_reason(
    a: SecurityDecision,
    b: SecurityDecision,
    *,
    a_universe_missing: bool = False,
    b_universe_missing: bool = False,
    a_blocked: bool = False,
    b_blocked: bool = False,
) -> ReasonCategory:
    """差异归因：根据两条链的决策上下文推断差异类别。

    规则顺序（优先级从高到低）：
      1) 若一边不在候选池 → MEMBER_SOURCE_DIFF
      2) 若一边 hard_gate 阻断 → HARD_GATE_INTERVENTION
      3) 一边 DATA_BLOCKED，另一边非 DATA_BLOCKED + Score 存在性不同 → SCORE_DATA_GAP
      4) BUY/SELL 但 reason_code 含 'threshold'/'coverage' → COVERAGE_THRESHOLD_DIFF
      5) 其他可预期 → EXPECTED_STRUCTURAL_DIFF（兜底已明确改口径的）
      6) 剩下来的先标 UNKNOWN_ENGINE_DIFF（P0 要求清零）
    """
    if a_universe_missing or b_universe_missing:
        return "MEMBER_SOURCE_DIFF"
    if a_blocked or b_blocked:
        return "HARD_GATE_INTERVENTION"
    if a.action == "DATA_BLOCKED" and b.action != "DATA_BLOCKED":
        return "SCORE_DATA_GAP"
    if b.action == "DATA_BLOCKED" and a.action != "DATA_BLOCKED":
        return "SCORE_DATA_GAP"
    rcodes = [a.reject_reason_code or "", b.reject_reason_code or ""]
    if any("coverage" in c.lower() or "threshold" in c.lower() for c in rcodes):
        return "COVERAGE_THRESHOLD_DIFF"
    if any("risk" in c.lower() for c in rcodes):
        return "RISK_CHECK_BLOCK"
    if any("pit" in c.lower() or "cutoff" in c.lower() for c in rcodes):
        return "PIT_CUTOFF_DIFF"
    return "UNKNOWN_ENGINE_DIFF"


def compare_one_day(
    a: DailyChainResult,
    b: DailyChainResult,
    *,
    portfolio_id: int = 0,
    top_k_symbol_diffs: int = 50,
) -> DailyDualRunReport:
    """对比单日两条链决策结果 → DailyDualRunReport。"""
    report = DailyDualRunReport(portfolio_id=portfolio_id, trade_date=a.trade_date)
    report.chain_a_id = _chain_id("A", a.trade_date, portfolio_id)
    report.chain_b_id = _chain_id("B", b.trade_date, portfolio_id)
    report.chain_a_meta = dict(a.meta or {})
    report.chain_b_meta = dict(b.meta or {})

    # ── 1) Universe ───────────────────────────────────────────────────────
    set_a = set(a.universe_symbol_ids)
    set_b = set(b.universe_symbol_ids)
    inter = set_a & set_b
    union = set_a | set_b
    report.universe_jaccard = (len(inter) / len(union)) if union else 1.0
    report.universe_added = sorted(set_b - set_a)   # B 独有（新来源新增）
    report.universe_removed = sorted(set_a - set_b)  # A 独有（旧来源移除）

    # ── 2) Actions 6x6 混淆矩阵 ───────────────────────────────────────────
    confusion = build_confusion_matrix_6x6()
    cat_counter: dict[str, int] = {}
    diffs_collector: list[dict[str, Any]] = []

    for sym_id in union:
        a_in = sym_id in set_a
        b_in = sym_id in set_b
        dec_a = a.decisions.get(sym_id, SecurityDecision(symbol_id=sym_id, action="NO_ACTION"))
        dec_b = b.decisions.get(sym_id, SecurityDecision(symbol_id=sym_id, action="NO_ACTION"))
        # 归一化 action：不在候选池但有决策的视为链内原生；
        # 不在链内且无决策的视为 NO_ACTION（链未涉及）
        aa = dec_a.action if a_in else "NO_ACTION"
        bb = dec_b.action if b_in else "NO_ACTION"
        aa = aa if aa in ACTION_TYPES else "NO_ACTION"
        bb = bb if bb in ACTION_TYPES else "NO_ACTION"
        confusion[aa][bb] += 1

        if aa != bb or not a_in or not b_in:
            reason = classify_diff_reason(
                dec_a, dec_b,
                a_universe_missing=not a_in,
                b_universe_missing=not b_in,
                a_blocked=(a.blocking_status != "READY"),
                b_blocked=(b.blocking_status != "READY"),
            )
            cat_counter[reason] = cat_counter.get(reason, 0) + 1
            if len(diffs_collector) < top_k_symbol_diffs:
                diffs_collector.append({
                    "symbol_id": sym_id,
                    "chain_a": {"in_universe": a_in, "action": aa,
                                "reject_code": dec_a.reject_reason_code},
                    "chain_b": {"in_universe": b_in, "action": bb,
                                "reject_code": dec_b.reject_reason_code},
                    "reason_category": reason,
                })
            # P0 计数：BUY/SELL 不一致 + 原因是 UNKNOWN / UNMAPPED
            is_trade_action = (aa in {"BUY", "SELL"} or bb in {"BUY", "SELL"})
            if is_trade_action and reason in {"UNKNOWN_ENGINE_DIFF", "UNMAPPED"}:
                report.p0_unexplained_count += 1
            # P1 计数：HOLD ↔ NO_ACTION 反转
            if {aa, bb} == {"HOLD", "NO_ACTION"}:
                report.p1_hold_noaction_flip += 1

    report.action_confusion = confusion
    report.inconsistent_by_category = cat_counter
    report.symbol_diffs = diffs_collector

    # 匹配率
    match_count = sum(confusion[x][x] for x in ACTION_TYPES)
    denom = sum(sum(row.values()) for row in confusion.values())
    report.action_match_rate = (match_count / denom) if denom else 1.0

    if report.p0_unexplained_count > 0:
        report.warnings.append(
            f"P0 未解释交易差异 {report.p0_unexplained_count} 条，需归因后清零进 G6。"
        )
    if report.p1_hold_noaction_flip > 0:
        report.warnings.append(
            f"P1 HOLD/NO_ACTION 方向反转 {report.p1_hold_noaction_flip} 次，建议检查候选池。"
        )
    return report


# ============================================================================
# 20 交易日加速回放主入口
# ============================================================================
ChainRunner = Callable[[date, Literal["A", "B"]], DailyChainResult]


def run_g5_dual_run_replay(
    *,
    portfolio_id: int,
    chain_runner: ChainRunner,
    anchor_date: date | None = None,
    n_days: int = 20,
    explicit_dates: list[date] | None = None,
) -> G5ReplaySummary:
    """G5 加速回放：对 20 个交易日分别跑 chain A/B，汇总对比报告。

    Args:
        portfolio_id: 组合 id
        chain_runner: 单链单天执行函数 (trade_date, chain) -> DailyChainResult
                     真实生产里：chain A = 旧来源 evaluate；chain B = 新来源 evaluate
                     （注意：均不写订单表，仅构造决策快照返回）
        anchor_date: 回放终止日（最近的交易日）；若 explicit_dates 给了则忽略
        n_days: 默认 20（G6 要求的灰度周期等价长度），AC-15 基线是 ≥10
        explicit_dates: 若提供则直接使用（便于精准复现特定 20 日窗口）
    """
    if explicit_dates:
        dates = sorted(set(explicit_dates))
    else:
        anchor = anchor_date or date.today()
        dates = generate_20_trade_days_backward(anchor, n_days=n_days)

    start_date, end_date = dates[0], dates[-1]
    summary = G5ReplaySummary(
        portfolio_id=portfolio_id, start_date=start_date, end_date=end_date,
        total_days=len(dates),
    )

    amr_sum, uj_sum = 0.0, 0.0
    for idx, td in enumerate(dates):
        try:
            result_a = chain_runner(td, "A")
            result_b = chain_runner(td, "B")
        except Exception as exc:
            logger.exception("G5 replay day %s failed: %s", td, exc)
            summary.skipped_days.append(td)
            summary.summary_notes.append(f"day={td.isoformat()} failed: {type(exc).__name__}: {exc}")
            continue
        daily = compare_one_day(result_a, result_b, portfolio_id=portfolio_id)
        summary.daily_reports.append(daily)
        summary.days_replayed += 1
        amr_sum += daily.action_match_rate
        uj_sum += daily.universe_jaccard
        summary.total_p0_unexplained += daily.p0_unexplained_count
        summary.total_p1_hold_noaction_flip += daily.p1_hold_noaction_flip
        if daily.p0_unexplained_count > 0:
            summary.failing_days_p0.append(td)
        if daily.p1_hold_noaction_flip > 0:
            summary.failing_days_p1.append(td)

    n = max(1, summary.days_replayed)
    summary.avg_action_match_rate = amr_sum / n
    summary.avg_universe_jaccard = uj_sum / n

    summary.g5_eligible_for_g6 = (
        summary.total_p0_unexplained == 0
        and summary.total_p1_hold_noaction_flip == 0
        and summary.days_replayed >= 10  # AC-15 至少 10
        and not summary.skipped_days  # 连续窗口不得存在漏跑/故障日
    )
    if summary.g5_eligible_for_g6:
        summary.summary_notes.append(
            f"G5 通过（{summary.days_replayed} 日，P0/P1 未解释差异清零）→ 准入 G6。"
        )
    else:
        summary.summary_notes.append(
            "G5 未通过：仍有未解释差异或有效回放天数不足 10 → 不可进入 G6。"
        )
    return summary


# ============================================================================
# 便利：构造 QA/测试用「合成 chain_runner」（不访问真实 DB，便于 pytest 验证框架）
# ============================================================================
def build_synthetic_chain_runner(
    *,
    seed_portfolio_id: int,
    universe_a: list[int],          # 旧来源固定候选集
    universe_b: list[int],          # 新来源固定候选集
    action_schedule: dict[str, dict[int, str]] | None = None,  # date_iso -> {sym_id: action}
    always_pass: bool = False,      # True = 两条链完全一致（always_pass 模式）
) -> ChainRunner:
    """构造「合成」单链 runner，用于 pytest 验证 G5 框架本身的正确性。

    真实生产中的 chain_runner 应当调用：
      - chain A: evaluate(persist=False, member_source=LEGACY_SCAN)
      - chain B: evaluate(persist=False, member_source=PORTFOLIO_CANDIDATE, enforce_pit=True)
    两者都只构造 SecurityDecision 快照，**绝不写订单表**。
    """
    schedule = action_schedule or {}

    def _runner(td: date, chain: Literal["A", "B"]) -> DailyChainResult:
        universe = list(universe_b) if (chain == "B") else list(universe_a)
        # 为了 always_pass：强制 universe_a == universe_b
        if always_pass and chain == "A":
            universe = list(universe_b)
        decisions: dict[int, SecurityDecision] = {}
        day_actions: dict[int, str] = schedule.get(td.isoformat(), {})
        for sym_id in universe:
            base_action = day_actions.get(sym_id, "HOLD")
            sd = SecurityDecision(
                symbol_id=sym_id, action=base_action,
                target_weight=0.05 if base_action == "BUY" else None,
                score_value=0.7 if base_action in {"BUY", "SELL", "HOLD"} else None,
            )
            # always_pass 模式：A/B 完全相同，否则在 B 链上对 A 独有标的加 DATA_BLOCKED 原因
            if not always_pass:
                if chain == "B" and sym_id in universe_a and sym_id not in universe_b:
                    # 实际上不会进 universe，这里只是防御性标注
                    sd.reject_reason_code = "MEMBER_SOURCE__EXCLUDED_FROM_PORTFOLIO_CANDIDATE"
                if base_action == "NO_ACTION" and (sym_id % 5 == 0):
                    sd.action = "DATA_BLOCKED" if (chain == "A") else sd.action
            decisions[sym_id] = sd
        return DailyChainResult(
            chain=chain, trade_date=td,
            universe_symbol_ids=universe,
            decisions=decisions,
            coverage_pct=96.0 if (chain == "B") else 88.0,
            blocking_status="READY",
            meta={"capture_mode": "synthetic_test"},
        )
    return _runner


__all__ = [
    # 数据结构
    "SecurityDecision", "DailyChainResult", "DailyDualRunReport", "G5ReplaySummary",
    "ACTION_TYPES", "ReasonCategory",
    # 主入口
    "compare_one_day", "run_g5_dual_run_replay", "generate_20_trade_days_backward",
    # 归因
    "classify_diff_reason",
    # QA
    "build_synthetic_chain_runner",
]
