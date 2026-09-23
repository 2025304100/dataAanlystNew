"""阻断原因文案分层 · 回归哨兵（2026-09-22）。

问题（用户视角实测）：筛选面板把**开发者口径**的文案直接渲染给用户，例如
「停牌状态无字段、无事件表。`security_status_daily` 不可用（见 `_BLOCKED_STATUS_TABLE`）。」
—— 表名、常量名、fixture 名出现在用户界面上。

本卡把文案拆成两层：
- `blocked_reason_zh`：**给用户看**的句子（为什么不能用、怎么才能用）
- `blocked_detail_zh`：**给开发看**的口径（表名/常量/实测行数），前端放悬浮提示

哨兵：任何 `blocked_reason_zh` 混进内部标识都直接失败。
"""
from __future__ import annotations

import re

import pytest

from app.services.factors.candidate_pool.rules import FIELD_BINDINGS

#: 内部标识特征：反引号包裹、表名模式、常量名、fixture 名、列名
_INTERNAL_PATTERNS = [
    r"`",                      # 反引号（内部标识的典型包装）
    r"\braw_[a-z_]+",          # raw_xxx 物理表
    r"\b[a-z_]+_snapshots\b",  # xxx_snapshots
    r"\bsecurity_status_daily\b",
    r"\bindex_constituents\b",
    r"\buniverse_symbols\b",
    r"\bsymbols\b",
    r"_TABLE\b",               # _BLOCKED_STATUS_TABLE 之类常量
    r"_FIXTURE\b",
    r"\bFIXTURE\b",
    r"\bas_of_batch_id\b",
    r"\bstatus_source\b",
    r"\blisted_at\b",
    r"\bnet_profit(_yoy)?\b",
    r"\bdividend_yield\b",
]


def _blocked_bindings():
    return [b for b in FIELD_BINDINGS.values() if b.blocked]


def test_there_are_blocked_fields_to_check():
    """前置：本哨兵必须真的覆盖到字段，别因为"没有 blocked 字段"而空跑通过。"""
    assert _blocked_bindings(), "FIELD_BINDINGS 里没有 blocked 字段 —— 哨兵失去意义，请检查用例"


@pytest.mark.parametrize("binding", _blocked_bindings(), ids=lambda b: b.field)
def test_blocked_reason_is_user_facing(binding):
    """用户句不得含任何内部标识（表名 / 常量名 / fixture / 列名 / 反引号）。"""
    reason = binding.blocked_reason_zh or ""
    assert reason, f"{binding.field}: 阻断字段必须给出用户可读的原因"
    hits = [p for p in _INTERNAL_PATTERNS if re.search(p, reason)]
    assert not hits, f"{binding.field}: blocked_reason_zh 泄漏内部标识 {hits}：{reason}"


@pytest.mark.parametrize("binding", _blocked_bindings(), ids=lambda b: b.field)
def test_blocked_detail_kept_for_developers(binding):
    """开发口径不能被丢掉 —— 它要落在 blocked_detail_zh 上，供悬浮/日志使用。"""
    detail = binding.blocked_detail_zh or ""
    assert detail, f"{binding.field}: 阻断字段必须保留开发者口径（blocked_detail_zh）"


def test_to_dict_exposes_both_layers():
    """契约：`to_dict()` 同时输出用户句与开发句（前端据此分层渲染）。"""
    binding = _blocked_bindings()[0]
    payload = binding.to_dict()
    assert "blocked_reason_zh" in payload
    assert "blocked_detail_zh" in payload
