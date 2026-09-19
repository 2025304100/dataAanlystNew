"""pytest 插件：在 import 时摘掉 T06 新增的 3 个字段。

用途：定点判定 `test_preview_ep_formula` / `test_preview_conditional_formula`
的失败是否由 T06 的字段注册引起（而非 T05 的执行器改动或既有问题）。

用法：
    PYTHONPATH=.workbuddy/mining python -m pytest <target> -p _t06_drop_new_fields
"""

from __future__ import annotations

import app.services.factors.factor_compiler as _fc

_NEW = ("dividend_yield", "total_market_cap", "circulating_market_cap")

for _name in _NEW:
    _fc.FIELD_CATALOG.pop(_name, None)

# 同步重建派生集合，避免留下不一致的缓存
_fc._ALLOWED_FIELD_NAMES = frozenset(_fc.FIELD_CATALOG.keys())

print(f"[_t06_drop_new_fields] 已摘除 {_NEW}；FIELD_CATALOG -> {len(_fc.FIELD_CATALOG)} 项")
