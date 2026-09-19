"""一次性迁移：把三份原始需求文档补进 tasks.json 每个任务的 reads。

╔══════════════════════════════════════════════════════════════════════════╗
║ 状态：已于 2026-09-16 执行完毕（补入 91 条引用，覆盖 40/40 任务）。         ║
║ 保留本文件仅作审计用途（记录映射规则是怎么定的）。可安全删除。              ║
║ 本脚本幂等：重复执行不会重复添加。改 DOC_MAP 后可再次执行。                ║
╚══════════════════════════════════════════════════════════════════════════╝

背景：本排期最初只让 Agent 读 SD-v2.0，遗漏了三份原始文档：
  - docs/因子挖掘系统-开发需求文档-落地版.md   （需求规则：阈值、约束、验收）
  - docs/因子挖掘系统-开发文档-落地版.md       （技术落点：表结构、接口、算法实现）
  - docs/因子挖掘实验向导-详细设计.md          （UI 细节：交互、文案、布局）

映射规则：按任务性质取对应章节。只加「该信息只存在于原始文档」的引用；
SD-v2.0 已完整转述的内容不重复引用（避免 Agent 读两份）。

新增检查（在 _selfcheck_conflict.py 的 C13~C16）会校验：
  - reads 里的文档路径真实存在
  - 40/40 任务都引用了原始文档
  - source_docs.precedence 与 docs[].known_errors 完整
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS_FILE = HERE / "tasks.json"

REQ = "docs/因子挖掘系统-开发需求文档-落地版.md"
DEV = "docs/因子挖掘系统-开发文档-落地版.md"
WIZ = "docs/因子挖掘实验向导-详细设计.md"

# task_id -> 需要补的原始文档章节
DOC_MAP: dict[str, list[str]] = {
    # ── PH0 ──
    "T01": [f"{DEV}#2.1 直接复用清单", f"{DEV}#2.2 建议新增文件", f"{REQ}#6.4 新增落库字段"],
    "T02": [f"{REQ}#3.2 过拟合防线·数据层", f"{DEV}#3.16 三段切分接入"],

    # ── PH1 DSL ──
    "T03": [f"{REQ}#6.0 DSL 扩展", f"{DEV}#3.14 DSL 扩展实现"],
    "T04": [f"{REQ}#6.0 新增时序算子", f"{DEV}#3.14 新增算子表"],
    "T05": [f"{REQ}#6.0 方言归一", f"{DEV}#3.14 实现步骤 4/6"],
    "T06": [f"{REQ}#6.1 两层结构·第一层", f"{WIZ}#6.3.3 25 个经典因子模板清单",
            f"{DEV}#3.6 因子公式模板"],

    # ── PH2 候选池 ──
    "T07": [f"{DEV}#3.0.1 候选池隔离模型", f"{REQ}#1.1 候选池看板交互"],
    "T08": [f"{WIZ}#3.6 候选池结果可视化", f"{DEV}#3.0.1 候选池隔离模型"],
    "T09": [f"{WIZ}#3.2 条件过滤页布局", f"{WIZ}#3.3 过滤条件与数据边界",
            f"{WIZ}#3.4 近三年亏损口径"],
    "T10": [f"{WIZ}#3.1 方式 A：外部导入"],
    "T11": [f"{WIZ}#3.7 生成挖掘物料与看板", f"{WIZ}#3.7.4 看板弹窗布局",
            f"{WIZ}#3.7.5 与 AI 生成联动"],

    # ── PH3 任务与锁 ──
    "T12": [f"{REQ}#3.1 双任务锁", f"{DEV}#3.15 双锁实现", f"{WIZ}#8.2 双任务锁"],
    "T13": [f"{DEV}#5 任务执行与性能", f"{REQ}#3.1 提交流程"],
    "T14": [f"{REQ}#3.5 字段异步校验交互", f"{WIZ}#5 字段与校验",
            f"{WIZ}#5.1 校验结果与修复回流"],
    "T15": [f"{WIZ}#10 数据镜像管理", f"{WIZ}#10.2 覆盖偏差控制"],

    # ── PH4 进化闭环 ──
    "T16": [f"{WIZ}#7 提交时资源确认与最终校验", f"{WIZ}#4 Step2 时间与目标"],
    "T17": [f"{REQ}#6.1 候选生成·两层结构", f"{WIZ}#6.3.1 两层结构",
            f"{WIZ}#6.3.4 经典底座生成逻辑"],
    "T18": [f"{REQ}#6.1 受约束随机", f"{WIZ}#6.3.6 受约束随机生成逻辑"],
    "T19": [f"{REQ}#6.3 去重与人工筛选", f"{WIZ}#6.3.8 四层去重机制"],
    "T20": [f"{DEV}#3.13 G2 子表达式缓存", f"{WIZ}#6.11.1 G2 公共子表达式缓存"],
    "T21": [f"{DEV}#3.2.2 探针字段", f"{WIZ}#6.11.2 性能探针"],
    "T22": [f"{REQ}#3.3 AI 生成约束", f"{DEV}#3.7 初始种群三来源·AI 生成",
            f"{WIZ}#6.3.9 AI 生成配置与自由度约束"],
    "T23": [f"{REQ}#6.1 逐代进化", f"{DEV}#3.9 繁殖与自适应实现(V3.16 切分)",
            f"{REQ}#3.2 过拟合防线"],
    "T24": [f"{DEV}#3.16 三段切分接入", f"{REQ}#3.2 第一层数据层", f"{WIZ}#8.3.7 最终验证"],
    "T25": [f"{REQ}#6.4 生命周期与数据落库", f"{WIZ}#8.4 结果总览·后续操作"],
    "T26": [f"{REQ}#6.7 F1 历史经验库", f"{DEV}#3.12 F1 历史经验库"],

    # ── PH5 前端 ──
    "T27": [f"{REQ}#3.4 前端与 i18n", f"{DEV}#6 前端实现要求",
            f"{WIZ}#2 步骤条交互"],
    "T28": [f"{WIZ}#3 Step1 候选股票池", f"{WIZ}#3.7.2/3.7.3 按钮与锁定交互"],
    "T29": [f"{WIZ}#4 Step2 时间与目标", f"{WIZ}#4.1 数据可用区间提示与镜像引导"],
    "T30": [f"{WIZ}#5 Step3 字段与校验"],
    "T31": [f"{WIZ}#6 Step4 进化参数", f"{WIZ}#6.5.5 Step4 配置界面",
            f"{WIZ}#7 提交时资源确认与最终校验"],
    "T32": [f"{WIZ}#8 Step5 进化执行与结果", f"{WIZ}#8.3 进化执行界面",
            f"{WIZ}#8.4 结果总览"],

    # ── PH6 M2 ──
    "T33": [f"{REQ}#6.1.1 选择机制", f"{WIZ}#6.5 选择机制 A1+B1+B2"],
    "T34": [f"{REQ}#6.1.2 繁殖与自适应", f"{WIZ}#6.6 繁殖与自适应机制"],
    "T35": [f"{REQ}#3.2 第二层统计层", f"{DEV}#3.17 统计层实现"],
    "T36": [f"{REQ}#6.8 因子质量分级", f"{DEV}#3.18 质量分级实现"],
    "T37": [f"{WIZ}#8.4.1 因子详情证据抽屉", f"{WIZ}#8.4.2 人工调整等级",
            f"{WIZ}#8.4.3 季度重评交互", f"{WIZ}#6.9.7 F1 列表页设计"],

    # ── PH7 ──
    "T38": [f"{DEV}#3.13 G1 多进程并行", f"{REQ}#6.1.6 性能 G2/G1"],
    "T39": [f"{DEV}#6 前端实现要求"],
    "T40": [f"{REQ}#3.4 en-US 补齐时点"],
}


def main() -> int:
    doc = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in doc["tasks"]}

    # 1) 补进任务卡顶部：原始三份文档的定位与优先级
    doc["source_docs"] = {
        "precedence": [
            "技术实现细节（常量/签名/表结构/算法） -> 以 SD-v2.0 为准，它已按代码实测勘误",
            "需求规则 / UI 细节（阈值/文案/交互/布局） -> 以三份原始文档为准",
            "两者冲突时（已知 4 处，见 SD-v2.0 附录 B）-> 一律以 SD-v2.0 为准",
            "三份原始文档互相冲突时 -> 需求文档 > 开发文档 > 向导设计（需求 > 技术 > UI 表述）"
        ],
        "docs": [
            {
                "path": REQ,
                "role": "需求规则唯一来源：阈值、约束、验收标准、字段可用性",
                "read_when": "需要确认某个数值/约束/验收口径时",
                "known_errors": [
                    "§2.1 月频 val/test 写的 12/12 未扣 purge/embargo，实算 11/6 -> 以 SD-v2.0 §7.2.3 为准",
                    "§4.1 月频镜像 10 年 test 约 24 点，实算 18 -> 该门槛不可达，见 SD-v2.0 §12.2 Q3",
                    "§3.2 purge_days=5 未说明单位 -> 必须经 normalize_purge_points 换算，见 SD-v2.0 §7.2.2"
                ]
            },
            {
                "path": DEV,
                "role": "技术落点来源：表结构、接口契约、算法实现步骤、复用清单",
                "read_when": "需要确认某能力在哪个文件/什么签名时",
                "known_errors": [
                    "§4 错误契约写 fix_action{...}，代码实际是 fix_link + extras.fix_action -> 以 SD-v2.0 §8.1 为准",
                    "§3.16 未给出 min_validation_days 显式传参，会 hard_fail -> 以 SD-v2.0 §7.2.2 为准",
                    "§6 说挖掘入口在因子中心内，与用户要求的「设置新增一栏」不一致 -> 以 SD-v2.0 §1.2 D1 裁决为准"
                ]
            },
            {
                "path": WIZ,
                "role": "UI 细节唯一来源：交互流程、锁定态、文案、图表布局、向导每一步",
                "read_when": "实现向导任一步骤或结果页 UI 时",
                "known_errors": [
                    "§3.7.4 看板示意中的示例数值仅为示意，不要当常量",
                    "§6.5/§6.6 描述的是 M2 机制，M1 阶段不要实现"
                ]
            }
        ]
    }

    # 2) 把原始文档章节补进各任务的 reads（幂等）
    added, skipped, missing_ids = 0, 0, []
    for tid, refs in DOC_MAP.items():
        task = tasks.get(tid)
        if task is None:
            missing_ids.append(tid)
            continue
        reads = task.setdefault("reads", [])
        for ref in refs:
            if ref in reads:
                skipped += 1
                continue
            reads.append(ref)
            added += 1

    if missing_ids:
        print(f"[WARN] DOC_MAP 里有不存在的任务 ID: {missing_ids}")

    tasks_without_source = [
        t["id"] for t in doc["tasks"]
        if not any(("开发需求文档" in r or "开发文档-落地版" in r or "实验向导" in r)
                   for r in t.get("reads", []))
    ]

    TASKS_FILE.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"已补 {added} 条原始文档引用，跳过 {skipped} 条（已存在）")
    print(f"覆盖任务数: {len(DOC_MAP)}/{len(doc['tasks'])}")
    if tasks_without_source:
        print(f"[WARN] 仍未引用原始文档的任务: {tasks_without_source}")
    print(f"已写入 {TASKS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
