# -*- coding: utf-8 -*-
"""前端卡 writes 自动补 i18n（一次性固化 T28~T37 反复出现的问题）。

背景：T27 之后每张前端卡的 writes 都只声明了组件目录，**漏了 i18n 两份**，
而前端文案必须走 t()、`translations.test.ts` 又要求中英 key 集合严格相等 →
每张卡都要人工补登记（连续 7 张）。本脚本把该动作固化：

- 扫描 tasks.json：任一卡 writes 含 `frontend/src/components/` 但缺 i18n 两份 → 自动补；
- `--check` 只报告不修改（供 selfcheck/CI 用）；
- 幂等：已补齐的卡不再重复添加。

用法：
    python .workbuddy/mining/_card_i18n_autofix.py          # 自动补齐
    python .workbuddy/mining/_card_i18n_autofix.py --check  # 只检查（exit 1 表示有卡待补）
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
TASKS = ROOT / ".workbuddy/mining/tasks.json"

I18N = ["frontend/src/i18n/zh-CN.ts", "frontend/src/i18n/en-US.ts"]
FRONTEND_PREFIX = "frontend/src/components/"


def main() -> int:
    check_only = "--check" in sys.argv
    data = json.loads(TASKS.read_text(encoding="utf-8"))
    pending: list[tuple[str, list[str]]] = []

    for card in data["tasks"]:
        writes = card.get("writes") or []
        has_frontend = any(w.startswith(FRONTEND_PREFIX) for w in writes)
        if not has_frontend:
            continue
        missing = [f for f in I18N if f not in writes]
        if missing:
            pending.append((card["id"], missing))
            if not check_only:
                writes.extend(missing)

    if check_only:
        if pending:
            print("以下卡片缺 i18n 写权限（需运行不带 --check 的版本补齐）：")
            for cid, missing in pending:
                print(f"  {cid}: 缺 {missing}")
            return 1
        print("ALL OK —— 所有前端卡的 writes 均含 i18n 两份")
        return 0

    if pending:
        TASKS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        print("已补齐以下卡片的 i18n 写权限：")
        for cid, missing in pending:
            print(f"  {cid}: +{missing}")
    else:
        print("无需补齐（所有前端卡已含 i18n 两份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
