# -*- coding: utf-8 -*-
"""登记 TD-FE-RED-2（C 类 22 项逐项定性的技术债卡）+ G5 收口记录。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
debt = prog.setdefault("debt", {})

# ── C 类：单独立卡（用户拍板方案①）────────────────────────────────
debt["TD-FE-RED-2"] = {
    "title": "C 类前端红逐项定性（测试过时 vs 组件回归）",
    "opened_at": "2026-09-19",
    "status": "open",
    "authorized_by_user": "方案① —— 单独立技术债卡逐项定性",
    "scope": {
        "failed_tests": 22,
        "failed_files": 7,
        "note": "**独立立卡，不计入 47 张排期卡分母**（避免污染进度统计）；"
                "完成标准：22 项全部定性 + 按结论处置（测试过时→改断言；组件回归→修组件）",
        "files": [
            "src/components/__tests__/ExternalDataSync.test.tsx",
            "src/components/__tests__/FactorEvaluationLab.test.tsx",
            "src/components/__tests__/FactorLibrary.test.tsx",
            "src/components/__tests__/FactorModelPage.test.tsx",
            "src/components/__tests__/FactorModelSettings.test.tsx",
            "src/components/factors/__tests__/FactorModelPage.collections.test.tsx",
            "src/components/factors/__tests__/FactorModelPage.t13-supplement.test.tsx",
        ],
    },
    "method": [
        "1) 逐文件跑 vitest 单文件，收集失败断言原文（期望值 vs 实际值）",
        "2) 定性三选一：[测试过时] 组件行为已按后续卡演进、断言语义陈旧 → 更新断言；"
        "[组件回归] 组件偏离既定口径 → 修组件（并补测试）；[口径未定] → 记录并上报",
        "3) 每项处置后跑该文件 + 全量，确保失败数单调下降、无新增失败",
    ],
    "guardrails": [
        "禁止批量把断言改成「迁就现状」（会掩盖真实回归）",
        "每项处置必须在 evidence 留「定性依据」一句（为什么判定为过时/回归）",
    ],
    "opened_when": "TD-FE-RED（A 类）修复完成后",
    "blocks": "T39（拆分 Settings.tsx）DoD 要求 npm test 全绿",
    "updated_at": NOW,
}

# ── G5 收口记录 ─────────────────────────────────────────────────────
debt["G5-WIRING"] = {
    "title": "G5 收口：MiningShell 接线 step1~step5",
    "opened_at": "2026-09-19",
    "status": "done",
    "authorized_by_user": "用户明确授权修改 MiningShell.tsx（T27 写权限之外）",
    "what": (
        "T27 只交付了壳与步骤条（5 步键位齐全但内容为「待开放」占位）。"
        "本次接线：步骤条改为可点击跳转（data-mining-step-jump）+ 底部「上一步/下一步」"
        "（data-mining-prev/next，首尾步禁用）+ 按 currentStep 渲染 step1~step5；"
        "step5 需运行数据，无 runProgress 时显示空态（**不臆造进度**）。"
        "新增 i18n 3 key（miningWizardPrev/Next/NoRun，两份同步）。"
    ),
    "files": [
        "frontend/src/components/factors/mining/MiningShell.tsx",
        "frontend/src/components/factors/mining/wizard/MiningWizardFlow.test.tsx",
        "frontend/src/i18n/zh-CN.ts",
        "frontend/src/i18n/en-US.ts",
    ],
    "evidence": [
        "G5 测试 6 passed：默认第 1 步 + 5 步步骤条、连续「下一步」依次到达 5 个面板、"
        "首尾步按钮禁用、步骤条点击跳转、第 5 步空态（无数据不臆造）、"
        "有 runProgress 时渲染进化跟踪",
    ],
    "note": "此为一次性收口动作，未占用排期卡；改动跨越 T27 写权限，已获用户授权并在此留痕",
    "updated_at": NOW,
}

prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("已登记：TD-FE-RED-2（open）/ G5-WIRING（done）")
print("PROGRESS.debt keys:", list(debt.keys()))
