"""把 4 项全局裁决写进 PROGRESS.json，并给看板生成器加一节「已裁决项」。

状态：**已执行完毕（2026-09-16）**，可安全删除。保留是为记录改动方式与文本来源。

背景
----
2026-09-16 需求方拍板 4 项（SD-v2.0 §12.3）。这类决策**必须能被后续 Agent 读到**，
否则下一个 Agent 会重新想一遍、很可能想出不一样的结论（漂移）。
故写三处：
  1. `PROGRESS.json` 的 `global_decisions`（机器可读，唯一事实来源）
  2. `update_progress_doc.py` 渲染成看板 §9（人可读）
  3. 手册 §3.6 与 SD-v2.0 §12.3（文档）

本脚本幂等：重跑不会重复插入。
注意：本脚本用**文件**承载含反引号的字符串，不经 `bash -c` —— shell 会把反引号当命令替换吞掉。
"""
from __future__ import annotations

import json
import pathlib

PROGRESS = pathlib.Path(".workbuddy/mining/PROGRESS.json")
GENERATOR = pathlib.Path(".workbuddy/mining/update_progress_doc.py")

DECIDED_AT = "2026-09-16"
DECIDED_BY = "需求方"

DECISIONS = [
    {
        "id": "D-A",
        "topic": "单标的沙箱（indicator_ast_sandbox，高级筛选使用）对截面算子 cs_ 的处理",
        "resolution": "登记但拒绝",
        "decided_at": DECIDED_AT,
        "decided_by": DECIDED_BY,
        "enforcement": (
            "cs_* 名进 ALLOWED_FUNCS 以便白名单自省，但 _validate() 在构造期抛 ValueError "
            "并说明「需要面板上下文」。禁止改成静默返回 None；禁止为此给沙箱引入面板上下文（超 M1 范围）。"
        ),
        "source": "SD-v2.0 §12.3 D-A / 手册 §3.6",
    },
    {
        "id": "D-B",
        "topic": "「AST 节点标注计算方向」是否落库进 ExecutionPlan",
        "resolution": "不落库",
        "decided_at": DECIDED_AT,
        "decided_by": DECIDED_BY,
        "enforcement": (
            "方向只作运行时查询（FUNCTION_DIRECTION + collect_directional_calls）。"
            "禁止写进 to_canonical_json() / formula_ast —— 会让全量既有 "
            "factor_versions.execution_plan_hash 失效、同公式重复建版本。"
        ),
        "source": "SD-v2.0 §12.3 D-B / 手册 §3.6",
    },
    {
        "id": "D-C",
        "topic": "月频 S/A 通道门槛（原待决 Q3；需求 §4.1 要求 test>=24，实算 5 年 6 点 / 10 年 18 点，不可达）",
        "resolution": "月频永久最高 B 级",
        "decided_at": DECIDED_AT,
        "decided_by": DECIDED_BY,
        "enforcement": (
            "写入产品文案与设置页说明；日/周频的 test>=24 约束不动；"
            "代码禁止写死 24，门槛做成可配置项（DEFAULT_THRESHOLDS）。"
        ),
        "source": "SD-v2.0 §12.3 D-C / 手册 §3.6",
    },
    {
        "id": "D-D",
        "topic": "prev_close 虚注册（raw_daily_bars 无此列，且 app/services/factors/ 内无任何派生实现）",
        "resolution": "移出 FIELD_CATALOG",
        "decided_at": DECIDED_AT,
        "decided_by": DECIDED_BY,
        "enforcement": (
            "编译期报 field_not_in_catalog，用户改写 ref(close, 1)。需同步改："
            "既有测试 test_prev_close_is_a_derived_ready_field、前端字段面板、"
            "_FIELD_DATA_POLICIES['prev_close']。"
            "危害形态（实测）：执行器字段解析是 context.get(node.id)（factor_executor.py:838-842），"
            "prev_close 不在 context 中 -> 表达式得 None -> execute() 返回 pd.Series(nan)，"
            "即静默产出全 NaN 因子且不报错。"
        ),
        "source": "SD-v2.0 §12.3 D-D / 手册 §3.6",
    },
]

RENDER_FUNC = '''

def render_decisions(progress: dict) -> list[str]:
    """渲染已裁决项（**已拍板，不要再重开讨论**）。"""
    items = progress.get("global_decisions") or []
    if not items:
        return []
    out = ["## 9. 已裁决项（**已拍板，不要再重开讨论**）", ""]
    out.append(
        "> 完整版见 SD-v2.0 §12.3 与手册 §3.6。"
        "**若认为需要改动，必须先提出新证据并走一次裁决**，不得以「看起来更合理」为由直接改代码。"
    )
    out.append("")
    out.append("| # | 议题 | **裁决结果** | 依据 |")
    out.append("|---|------|------------|------|")
    for d in items:
        out.append(
            f"| **{esc(str(d.get('id', '')))}** "
            f"| {esc(str(d.get('topic', '')))} "
            f"| **{esc(str(d.get('resolution', '')))}** "
            f"| `{esc(str(d.get('decided_at', '')))}` {esc(str(d.get('decided_by', '')))} |"
        )
    out.append("")
    for d in items:
        enforcement = str(d.get("enforcement") or "").strip()
        if enforcement:
            out.append(f"- **{esc(str(d.get('id', '')))} 遵守要求**：{esc(enforcement)}")
    out.append("")
    out.append(f"> 裁决来源：{esc(str(items[0].get('source', '')))}" if items else "")
    out.append("")
    return out
'''

HEADER_HINT = (
    '        f"> 生成时间：{now}　｜　账本更新人：`{progress.get(\'updated_by\', \'未知\')}`"\n'
    '        f"　｜　账本更新时间：`{progress.get(\'updated_at\', \'未知\')}`",\n'
    '        "",\n'
)


def main() -> int:
    # ── 1. PROGRESS.json ───────────────────────────────────
    progress = json.loads(PROGRESS.read_text(encoding="utf-8"))
    if "global_decisions" not in progress:
        progress["global_decisions"] = DECISIONS
        print("  PROGRESS.json += global_decisions (4 项)")
    else:
        existing_ids = {d.get("id") for d in progress["global_decisions"]}
        added = [d for d in DECISIONS if d["id"] not in existing_ids]
        progress["global_decisions"].extend(added)
        print(f"  PROGRESS.json global_decisions 已存在，补入 {len(added)} 项")
    PROGRESS.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ── 2. 看板生成器 ──────────────────────────────────────
    src = GENERATOR.read_text(encoding="utf-8")
    if "def render_decisions" not in src:
        anchor = "\n\ndef render_footer("
        assert anchor in src, "找不到 render_footer 锚点"
        src = src.replace(anchor, RENDER_FUNC + "\ndef render_footer(", 1)
        print("  update_progress_doc.py += render_decisions()")

        call_anchor = "    lines += render_footer(progress, idx)"
        assert call_anchor in src, "找不到 render_footer 调用锚点"
        src = src.replace(
            call_anchor,
            "    lines += render_decisions(progress)\n" + call_anchor,
            1,
        )
        print("  update_progress_doc.py build() 已接入")
    else:
        print("  update_progress_doc.py 已有 render_decisions（跳过）")

    if "已裁决项" not in src.split("def render_decisions")[0]:
        old_hint = (
            '        f"　｜　账本更新时间：`{progress.get(\'updated_at\', \'未知\')}`",\n'
            "        \"\",\n"
        )
        new_hint = (
            '        f"　｜　账本更新时间：`{progress.get(\'updated_at\', \'未知\')}`",\n'
            '        f"> 已裁决项 {len(progress.get(\'global_decisions\') or [])} 项，'
            '见 §9（**已拍板，不要再重开讨论**）",\n'
            "        \"\",\n"
        )
        if old_hint in src:
            src = src.replace(old_hint, new_hint, 1)
            print("  render_header 已加已裁决项提示")

    GENERATOR.write_text(src, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
