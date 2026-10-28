# 评分链路改造 · AI 执行提示词

> 配套文档：`评分链路改造-实施规格.md`
> 用法：按顺序逐段投喂，每段一次对话轮次。**不要一次性全部投喂。**

---

## 使用流程

```
① 主控提示词  →  拿到侦察报告，人工确认 GATE 结论
② Phase A     →  状态语义修正（5 个任务）
③ Phase B     →  口径可见化（3 个任务，B3 需二次确认）
④ Phase C     →  口径闭环（5 个任务，C5 需二次确认）
⑤ 验收提示词   →  交付前自检
```

任一步失败 → 用「异常处理」提示词，不要继续往下走。

---

## ① 主控提示词

```text
你在 D:/ai_project/dataAanlystNew 仓库工作。本次任务：按《评分链路改造-实施规格.md》完成评分链路改造。

现在只做侦察，不写任何代码。

1. 完整读取 D:/ai_project/dataAanlystNew/评分链路改造-实施规格.md
2. 核对 §2 事实基线 F-01 ~ F-23：逐条打开证据文件，确认描述与实际代码一致
3. 执行 GATE-1：对 MySQL 库 gpfx（配置见 config/*.json）执行规格中的 SQL。
   若本机无 MySQL 驱动，不要安装任何东西，直接标注「需人工提供」
4. 执行 GATE-2：阅读 frontend/src/components/opportunity/DataPrepActions.tsx 的 retryOrStart
   及其后端调用链，判定是「支持续跑」还是「从头重跑」

输出格式，严格遵守，不要加别的内容：

## Facts 核对
| ID | 一致 | 差异说明 |

## GATE-1
分组数据，或「需人工提供」

## GATE-2
结论：支持续跑 / 从头重跑
依据：引用具体代码行

## 规格与代码的冲突
没有就写「无」

输出后停止，等待确认。不要进入 Phase A。
```

---

## ② Phase A 提示词

```text
执行 Phase A（TASK-A1 ~ A5），严格按规格 §4 给出的代码片段替换，不要自行改写实现方式。

前置：GATE-2 已完成。A5 的按钮文案按 GATE-2 结论取「继续筛选」或「重新筛选」。

第一步，跑基线测试并记录通过数：
  cd frontend && npx vitest run src/components/__tests__/HistoryInitSection.outcomes.test.tsx
  cd frontend && npx vitest run src/components/__tests__/Discovery.test.tsx

然后 A1 → A2 → A3 → A4 → A5 逐个执行。每个任务必须：改代码 → 跑该任务的 VERIFY → 通过才进下一个。
每完成一个，更新规格 §7 状态表（状态 / 完成时间 / 备注）。

完成条件：
- 5 个任务全部 VERIFY 通过
- 基线测试通过数不低于改造前
- 改动文件仅限规格列出的文件

输出：改动文件清单、每个任务的断言验证结果、测试通过数前后对比。

任一 VERIFY 失败：回滚该任务 → §7 标 FAILED → 停止并报告。
不要绕过，不要放宽断言，不要跳过用例。
```

---

## ③ Phase B 提示词

```text
执行 Phase B（TASK-B1 ~ B3）。

前置：GATE-1 已完成。B2 的形态由 GATE-1 判据决定，不要自行选择。

顺序：
1. B1：先实现接口，用 curl 验证返回结构（至少覆盖单版本、多版本两种情况），再进 B2
2. B2：形态严格按 GATE-1 结论执行，三种形态：
   - NULL 占比 < 10%  → 全区间展示
   - 10% ~ 50%        → 只展示最近 90 天，历史折叠为「含 N 条未知口径」
   - > 50%            → 停止 B2，改为在 B1 返回体加 unknown_count，仅在覆盖诊断页显示一行
3. B3：标了 [NEEDS-RECON]。先只输出侦察结论（规格列出的 3 个问题），停下等确认，不要直接改代码

完成条件：
- 单版本时不出现橙色告警（验收 V-6）
- 多版本时显示「检测到 N 个口径共存」（验收 V-7）

输出：接口 curl 结果、B2 的 DOM 断言或截图、B3 侦察结论。
```

---

## ④ Phase C 提示词

```text
执行 Phase C（TASK-C1 ~ C5）。

依赖：C1 → C2，C4 依赖 B1，C3 独立，C5 标 [NEEDS-RECON]。

硬性要求（违反即视为失败）：
- C1：scoring_config_version 为 null 时必须渲染「口径未知」，禁止隐藏。
  隐藏会让用户误判整列为同一口径，比显示未知更危险（规格禁止事项第 8 条）
- C2：结果集为单一口径（含全部为 null）时不渲染提示条，避免制造噪音
- C4：接口失败时静默降级，不得阻塞主流程
- C5：先只输出侦察结论（NotificationOutbox 写入惯例 / 事件源 / 策略覆盖），停下等确认

输出：每个任务的断言验证结果 + 多口径场景的构造方式（怎么造出两个版本的数据来验证）。
```

---

## ⑤ 验收提示词

```text
对本次改动做只读自检。不要修改任何文件。

1. 逐条核对规格 §6 验收总表 V-1 ~ V-14，每项标注：
   通过 / 未通过 / 无法自动验证（并说明人工怎么验）
2. 执行 git diff --stat，列出所有改动文件，标出不在规格文件清单里的
3. 检查 i18n：是否新增了规格未列出的键（对照规格各 TASK 里列出的键名）
4. 对照规格 §8 禁止事项 10 条，逐条确认没有违反

输出一张表 + 一句结论：这次改动是否可以交付。若不可，列出阻塞项。
```

---

## ⑥ 异常处理 · VERIFY 失败

```text
VERIFY 失败了。现在不要改代码。

1. 贴出失败的断言原文和完整报错
2. 判定属于哪一类：
   a) 实现写错了 → 给出修正方案
   b) 断言与代码现状不符（即规格写错了）→ 给出证据和建议
   c) 测试环境问题 → 说明如何排除
3. 等我决定。不要自行判定为 (a) 然后继续。

禁止：为了让测试变绿而放宽断言、跳过用例、删除断言、改 mock 让断言失效。
```

---

## ⑦ 异常处理 · 规格与代码冲突

```text
你发现规格与代码不一致。按以下规则处理：

- 事实层面（文件路径、行号、字段名、函数名、类型定义）→ 以代码为准，规格作废
- 行为层面（该显示什么文案、该用什么颜色、该走哪个分支、该在什么条件下渲染）→ 以规格为准
- 拿不准的 → 停下问我

输出：冲突点 / 代码实际是什么 / 规格怎么写的 / 你的建议。
```

---

## ⑧ 单轮执行版（仅在 Agent 不支持多轮确认时使用）

```text
一次性完成 D:/ai_project/dataAanlystNew/评分链路改造-实施规格.md 中 Phase A + Phase B + Phase C 的全部可执行任务。

跳过所有 [NEEDS-RECON] 与 [NEEDS-DESIGN] 标记的任务，只做明确可执行的。

规则：
1. 每个 TASK 完成立即跑 VERIFY，失败立刻停止，不要继续后面的 TASK
2. 不要跳过 TASK，不要合并 TASK，不要调整顺序
3. 全部结束后更新 §7 状态表
4. 输出：完成清单 / 失败清单 / 改动文件清单 / 未执行清单（及原因）

注意：若 GATE-1 无法执行（无数据库驱动），B2 跳过并在未执行清单里说明原因，不要凭猜测实现。
```

---

## 附：上下文注入块

若目标 Agent 无法读取本地文件（如在网页版对话中），把下面这段连同具体 TASK 一起投喂。

```text
【项目上下文】
- 仓库：D:/ai_project/dataAanlystNew
- 前端：React 18 + TypeScript + Vite + Ant Design，源码在 frontend/src
- 后端：FastAPI + SQLAlchemy + MySQL(gpfx)
- 前端验证：cd frontend && npx tsc -b --noEmit / npx vitest run <path>
- 后端验证：python -m pytest <path> -q

【本次改造的核心事实】
1. 评分预设已有版本链：ScoringConfig(preset_key, version, is_active, is_latest)
2. 每条评分已落库口径字段：Score(scoring_asset_type, scoring_config_id, scoring_preset_key, scoring_preset_name, scoring_config_version)
3. 候选结果接口已返回 scoring_preset_name / scoring_config_version
4. 详情弹窗已展示口径，但候选列表没有
5. 补数任务的 cancelled 状态被错误渲染为红色「失败」

【三条硬约束】
- 终态 cancelled/failed/expired/partial 必须有独立视觉表达，不得混用
- 同一屏不得出现两个分母的进度指标
- 口径为 null 时必须显示「口径未知」，禁止隐藏
```
