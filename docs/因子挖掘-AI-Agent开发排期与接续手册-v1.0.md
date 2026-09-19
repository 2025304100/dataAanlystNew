# 因子挖掘 · AI Agent 开发排期与接续手册 v1.0

> 日期：2026-09-16 ｜ 状态：待启用
> **本文件的服务对象是 AI Agent，不是人类团队。** 它解决的核心问题只有一个：
> **一个全新的 Agent（零对话记忆、零上下文）如何接住上一个 Agent 的活，并做对。**
> 设计依据：《因子挖掘系统-系统设计文档-v2.0.md》（下称 **SD-v2.0**）。
> 配套机器可读文件：`.workbuddy/mining/tasks.json`（任务索引）、`.workbuddy/mining/PROGRESS.json`（状态账本）、`.workbuddy/mining/next_task.py`（任务选择器）。

---

## 0. 三句话说明本文件怎么用

1. **接手一个任务前**：跑 `.venv/Scripts/python.exe .workbuddy/mining/next_task.py`，按它输出的任务 ID 在 **§5** 找到任务卡，只读卡里 `reads` 列出的文件（它已按任务挑好原始文档章节）。
2. **任务做完**：跑完卡里 `DoD` 的**全部命令**且全部通过 → 更新 `PROGRESS.json`（`status=done` + `evidence` + `decisions`）→ 跑 `update_progress_doc.py` 刷新进度看板。**没跑命令不算完成。**
3. **遇到阻塞**：把 `status` 改成 `blocked`，填 `blocked_reason`，然后停下。**不允许自行绕过**（见 §2.5）。

**三份产出各给谁看**：

| 产出 | 给谁 | 怎么更新 |
|------|------|---------|
| `docs/因子挖掘-开发进度看板.md` | **人看**（进度、门禁、阻塞、变更日志） | 自动生成，**不要手工改** |
| `.workbuddy/mining/PROGRESS.json` | **机器读**（唯一事实来源） | Agent 开工/收工各写一次 |
| `next_task.py` 的输出 | **Agent 看**（我下一步做什么） | 只读，不用更新 |

---

## 1. 接手须知（开工前必读，约 5 分钟）

### 1.1 本机环境陷阱（**已实测，不是猜测**）

| 现象 | 应对 |
|------|------|
| **Bash 工具 PATH 损坏**：`ls` / `head` / `tail` / `dirname` / `find` 均 `command not found` | 不要用这些命令。文件操作用 **Glob / Grep / Read / Write / Edit** 原生工具；确需 shell 时只用 `cd X && <绝对路径可执行文件>` 形式 |
| **PowerShell 工具 stdout 被吞**（返回 exit 0 但无任何输出） | **不要用 PowerShell 工具**。需要跑 Python 直接用 Bash 调 venv 解释器 |
| **managed Python 3.13.12 没有 numpy/pandas** | 一律用项目 venv：`D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe` |
| 仓库根目录有 **100+ 个 `_dbg_*.py` / `_tmp_*.py` / `_patch_*.py` / `*.db` 垃圾文件** | 那是历史调试残留。**不要读、不要删、不要参考**，忽略即可 |

**跑 Python / 测试的正确姿势**：

```bash
" D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe" -c "..."          # 临时脚本
"D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe" -m pytest tests/ -q  # 跑测试
"D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe" -m alembic heads     # alembic
```

> Bash 里 `cd "D:/ai_project/dataAanlystNew" && ...` 是可用的；只是管道到 `tail`/`head` 会失败，**直接输出全文**。

### 1.2 文档地图与阅读顺序（**按需读，不要通读**）

**五份文档的分工**——每份只在自己那一列是权威：

| 文档 | 权威范围 | 什么时候读 |
|------|---------|-----------|
| `因子挖掘系统-开发需求文档-落地版.md` | **需求规则**：阈值、约束、验收标准、字段可用性 | 要确认某个数值/约束/验收口径时 |
| `因子挖掘系统-开发文档-落地版.md` | **技术落点**：表结构、接口契约、算法实现步骤、复用清单 | 要确认某能力在哪个文件/什么签名时 |
| `因子挖掘实验向导-详细设计.md` | **UI 细节**：交互流程、锁定态、文案、图表布局、向导每一步 | 实现向导任一步骤或结果页 UI 时 |
| `因子挖掘系统-系统设计文档-v2.0.md`（SD-v2.0） | **技术事实来源**：全部经代码实测，并已勘误前三份 | 实现细节一律以它为准 |
| 本手册 | **排期与接续**：任务切分、依赖、写权限、DoD | 只在开工/收工/阻塞时读 |

**优先级规则（`tasks.json` 的 `source_docs.precedence` 是机器可读版）**：

1. **技术实现细节**（常量/签名/表结构/算法）→ 一律以 **SD-v2.0** 为准。
2. **需求规则 / UI 细节**（阈值/文案/交互/布局）→ 以三份原始文档为准。
3. 两者冲突时 → **一律以 SD-v2.0 为准**（已知 4 处，见 §1.3）。
4. 三份原始文档互相冲突时 → **需求文档 > 开发文档 > 向导设计**。

> ⚠️ **第 3 条不是小事**。三份原始文档里有 8 处已知错误/过时表述，Agent 若不加辨别地照做，
> 会写出**跑不起来或结论不可信**的代码。已知错误清单见 §1.3，**动手前必看**。

**阅读顺序**：

| 顺序 | 读什么 | 读多少 |
|------|--------|--------|
| 1 | 本文件 §1（含 §1.3 已知错误）+ §2 + §3 | 全文（约 8 分钟） |
| 2 | SD-v2.0 §2（现状实测基线） | 只读 §2 |
| 3 | 你任务卡 `reads` 列出的章节 | **只读列出的**——它已按你的任务挑好原始文档章节 |
| 4 | 需要更细的实现步骤时，再扩读原始文档对应章节 | 单章 |

**不要读**：

| 文件 | 原因 |
|------|------|
| `docs/因子挖掘系统-底层架构设计文档-v1.0.md` | **已被 SD-v2.0 取代**，含 2 项 P0 错误（purge 单位、默认 `min_*`）。已加取代声明 |
| `docs/因子挖掘系统-工作安排与WBS-v1.0.md` | 面向 12 人人类团队，**排期与写权限口径与本手册不一致** |
| `docs/因子挖掘系统-开发计划文档-执行版-v1.0.md` | 面向 4+3 人人类团队，门禁是评审式而非可执行命令 |
| `docs/因子挖掘V1.0探索/` 整个目录 | 历史草稿 |
| `docs/因子挖掘-决策记录-v3.md`、`因子挖掘需求审核报告.md` | 仅供人回溯决策链，Agent 不需要 |
| 仓库根目录的 `_dbg_*` / `_tmp_*` / `_patch_*` 文件 | 历史调试残留，读了会被误导 |

### 1.3 原始文档的已知错误清单（**动手前必看**）

三份原始文档是需求来源，但下面这些点**已过时或与代码不符**。遇到时按「正确处理」列执行，不要"忠实还原文档"。

| # | 文档与位置 | 文档表述 | 为什么错（实测） | 正确处理 |
|---|-----------|---------|-----------------|---------|
| **E1** | 需求 §2.1 样本量表 | 月频 `val 12 / test 12` | 按 `n×ratio` 直算，**未扣 purge/embargo** | 实算 **val 11 / test 6** → SD-v2.0 §7.2.3 |
| **E2** | 需求 §4.1 | 月频镜像 10 年 `test 约 24 点`，S/A 门槛 `≥24` | 实算 10 年月频 test 仅 **18 点**，**该门槛不可达** | ✅ **已裁决（D-C / SD-v2.0 §12.3）**：月频**永久最高 B 级**；日/周频 `test≥24` 不动；**代码禁止写死 24**，做成可配置项 |
| **E3** | 需求 §3.2 / 开发 §3.16 | `purge_days=5`、`embargo_days=5` | 该参数是**传入 `all_dates` 的索引间隔**，不是交易日。挖掘传的是调仓点，直接传 5 会让月频"隔离 5 个月" | **必须**经 `normalize_purge_points()` 换算 → daily 5 / weekly 1 / monthly 1 |
| **E4** | 开发 §3.16 | 只给 `build_time_split(all_dates=..., purge_days=5, embargo_days=5)`，未传 `min_*` | 默认 `min_validation_days=50` 推出 `derived_min_total=260`，**周频 243 点与月频 60/120 点全部抛 `insufficient_dates`** | **必须**显式传 `min_validation_days`/`min_test_days`/`min_total_days`（用 `SPLIT_MINIMUMS`） |
| **E5** | 开发 §4 错误契约 | `fix_action{action,target_step,message}`、`related_id` | 代码实际是 `FactorSevenError.to_7field()` → `fix_link`（URL），**没有** `related_id`/顶层 `fix_action` | 用 `fix_link`；结构化动作放 `extras.fix_action` |
| **E6** | 开发 §6 / 需求 §3.4 | 挖掘入口在「设置→因子中心」内，**不新增顶级菜单** | 与用户明确要求「系统设置中新增一栏」不一致 | 按 SD-v2.0 §1.2 **D1 裁决**：设置左侧导航新增一栏「因子挖掘」；不新增 App 顶级 Tab |
| ~~**E7**~~ | 需求 §2.3 | `prev_close` 列在字段可用性表中 | **❌ 该勘误本身是错的**：`prev_close` 由执行器 `_select_field_expressions` 投影为 `LAG(close) OVER (...)`，是**可用的虚字段**（详见 §3.6 D-D 撤销说明） | **保留原状**，非错误项 |
| **E8** | 向导 §3.7.4 / §6.5 / §6.6 | 看板示例数值、"赛道竞争/NSGA-II/自适应"机制描述 | 示例数值仅为示意；A1/B1/B2/C1~D3 是 **M2** 机制 | 示例数值不要当常量；**M1 阶段不要实现这些机制** |

> 以上 8 处在 `tasks.json` 的 `source_docs.docs[].known_errors` 里有机器可读版本，
> 且 `next_task.py --task <ID>` 打印的 `reads` 已按任务挑好了章节。

### 1.4 三十七条铁律（违反即为 bug，评审直接打回）

| # | 铁律 |
|---|------|
| R1 | **禁止重新决策已冻结的内容**（§3）。契约里的常量/签名/字段名不可改，只能加可选字段 |
| R2 | **禁止修改 L1 既有函数签名**：`factor_compiler.py` / `factor_executor.py` / `factor_evaluator.py` / `factor_registry.py` / `factor_lifecycle.py` / `factor_set_service.py`。只允许加**可选参数**且默认值保持原行为 |
| R3 | **禁止新增第三方依赖**（`requirements.txt` 除外，且须在任务卡里明写）。Bonferroni/FDR 走自研，**不引 statsmodels** |
| R4 | **禁止用 Python 内置 `hash()`** 做缓存键（有随机化种子）——必须 SHA-256 |
| R5 | **禁止让 val/test 段进入适应度计算**。`evaluate_short()` 内部必须 `assert_no_leakage()` |
| R6 | **禁止 test 段回炉**：`test` 只在最终确认时评估一次，评估后 `run.status → succeeded` 不可逆 |
| R7 | **禁止把 `purge_days=5` 直接传给 `build_time_split`**。必须先经 `normalize_purge_points()`（见 §3.1） |
| R8 | **禁止依赖 `build_time_split` 的默认 `min_validation_days`**。必须显式传三个 `min_*` |
| R9 | **禁止在路由层（L5）出现阈值/切分/指标计算**。只做 DTO 转换与入参校验 |
| R10 | **禁止把稀疏字段填零或用最新快照回填历史** |
| R11 | **禁止在真实库上执行任何破坏性迁移命令**。真实库是 **MySQL `127.0.0.1/gpfx`（127 张业务表）**。`alembic downgrade base`、`drop_all`、`DROP DATABASE` 一律禁止；迁移验证**必须**用 `ALEMBIC_DATABASE_URL` 指向一次性 scratch 库。回滚验证只能用 `downgrade -1`。 |
| R12 | **禁止假设数据库是默认 SQLite**。`config/db_config.json` 的 `use_mysql=true` 优先，`settings.database_url`（SQLite 临时文件）**不会被使用**。任何涉及 DB 的操作前，先确认实际连的是哪个库。 |
| R13 | **新建 alembic 迁移必须满足三条**：① 每张表显式 `mysql_engine="InnoDB"`（本机 server 默认 MyISAM，而 `task_locks` 的双锁依赖事务）② 索引名与 ORM 一致（从 metadata 反推，不手写短名，否则同一索引建两遍）③ 列集合与 ORM 完全对齐。**收工前必须跑 `verify_schema_drift.py` 确认无漂移（列/索引/唯一约束/外键）** —— `_auto_align_all_schema` 会把这三类缺陷静默补齐，导致「应用能跑但迁移是错的」。<br>④ **外键也归这四条管（2026-09-16 T07 补）**：迁移里写了 `sa.ForeignKeyConstraint(...)` **不等于**库里真有外键 —— **MyISAM 会静默忽略 FK 定义**（不报错、不警告），而事后把引擎 CONVERT 成 InnoDB **不会补建外键**。实测：真实库 14 张表 FK=0，却因 `verify_schema_drift.py` 当时不比对索引之外的外键而漏检两年。**验证方式**：跑漂移检查（已含外键）+ 做**行为验证**（插孤儿行应被 1452 拒绝、硬删父行应级联），不能只看 `information_schema` —— 「定义存在」≠「被强制」。 |
| R14 | **写测试前先读懂 `tests/conftest.py` 的两道门禁**，否则会在开工第一步就炸：<br>① `_run_factor_import_gate`（import 时运行）：扫描 `sys.modules` 里每个模块的**源文本**，禁止出现 `app.models.factor_` / `app.services.factor_set_service` / `app.services.factor_usage_service` 前缀的 import 语句。注意是**字面文本匹配** —— 测试文件的注释、字符串里都不能出现 `import app.models.factor_` 这样的片段。<br>② `_p0_hard_import_gate`（session autouse）：扫描 `app/**/*.py`。`app/services/factors/` 与 `app/api/routes/factor_` 前缀**豁免**；但 `app/models/factor_mining.py`、`mining_candidate_pool.py`、`task_lock.py`、`factor_formula_template.py` **不在豁免名单**（只豁免了 `factor.py`/`factor_model.py`/`factor_runtime.py`/`factor_evaluation.py`/`factor_governance.py`/`factor_shadow.py`）。往这些文件加 import 前后都要自查。 |
| R15 | **`tests/services/**` 下不要创建 `__init__.py`**。与 `tests/e2e`/`tests/integration`/`tests/performance` 保持一致（pytest 按顶层模块导入，`tests/conftest.py` 仍生效）。此外每个任务的 `writes` 是**精确白名单**，多建一个文件就是越权。 |
| R16 | **禁止在 `PROGRESS.json` / `tasks.json` 里手工追加同名键**。加任务条目前**先搜一遍**是否已有占位条目。原因是 JSON 的**重复键取后者且不报错**，而任何「读 JSON → 改 → 写回」的脚本还会顺手**静默去重** —— 合起来就是**数据无声丢失**。<br>🚨 **真实事故（2026-09-16）**：T03 记录后留了占位 `"T04": {"status": "pending"}`，T04 开工时又在 `tasks` 开头插入同名键。Edit 更新的是开头那个（done），但解析时后面的 `pending` 覆盖它，随后一个改 observations 的脚本把重复键去重 → **T04 的完成记录整体消失**，而文件语法完全合法、看不出异常。<br>**强制动作**：任何改 JSON 的脚本，**写回之前**先跑 `.workbuddy/mining/_selfcheck_conflict.py`（C19 检测重复键）。 |
| R17 | **禁止用「被截断的搜索结果」证明某个东西不存在。** 搜 `文件内容` 时若带了 `head_limit` / `head` / 分页，结论只能是「前 N 条里有/没有」，**不能推出「全仓没有」**。<br>🚨 **真实事故（2026-09-16）**：T03 期用带 `head_limit` 的 Grep 找 `prev_close`，`factor_executor.py:674-685` 的命中落在截断之外，于是写下「`app/services/factors/` 内**无任何派生实现**」——该结论被写进 SD-v2.0 §12.3 成为裁决 D-D 的**前提**，而 D-D 的结论是「删除该字段」。实测发现它其实是**可用**的（`LAG(close) OVER (...)` 投影 + 真实数仓数值验证 + 2 个既有绿灯测试）。若照做会打破 2 个绿灯测试并移除一项工作中的能力。<br>**正确做法**：判断「不存在」要么用**无上限**搜索，要么直接写代码/查数据做**正向验证**。 |
| R18 | **改了哪个文件，就要跑覆盖该文件的测试。** 回归集合不能只放本轮新增测试 + 顺手几个白盒。<br>🚨 **真实事故（2026-09-16）**：T04/T05 都改了 `factor_executor.py`，但两轮的回归集合都只包含 `tests/services/factors/mining` + 3 个白盒 + `tests/factors`，**漏了 `tests/test_whitebox_factor_executor.py`** → 该文件里的 2 个既有红灯连续两轮未被发现（属默认套件，`pytest.ini` 未按 marker 排除 `whitebox`）。<br>**强制动作**：收工前按下表自查。<br>| 改了 | 必须跑 |<br>|---|---|<br>| `factor_executor.py` | `tests/test_whitebox_factor_executor.py` |<br>| `factor_compiler.py` | `tests/test_whitebox_factor_compiler.py` |<br>| `formula_catalog.py` | `tests/test_whitebox_formula_catalog.py` |<br>| `discovery_fast_scan.py` / `indicator_ast_sandbox.py` | `tests/test_whitebox_discovery_filter.py` |<br>| **`app/api/router.py`**（注册新路由） | 任一 import `api_router` 的白盒测试：`tests/test_whitebox_factor_runtime_api.py`、`tests/test_whitebox_portfolio_strategies_api.py`、`tests/test_whitebox_ai_audit.py`（`test_factor_routes_are_registered` 是**包含式**断言，新增路由通常不破，但仍要跑） |<br>| **`app/api/routes/*.py`**（新增/改端点） | 该模块的集成测试 + `tests/factors/`（同域） |<br>| 因子域任意 L1 | `tests/factors/` |<br>> 表全量版见 `.workbuddy/memory/MEMORY.md` §5.1。 |

| R19 | **禁止用「列存在」判断字段可用 —— 必须实测非空覆盖率。** 本项目已连续踩中三次（`prev_close`、`dividend_yield`、以及 T09 一次性发现的 `listed_at` / `industry` / `net_profit` / `net_profit_yoy` / `is_st`）：列在表里、但**全为空**，导致静默产出错误结果。新接一个字段前，先跑 `SELECT COUNT(col) FROM tbl`；把结论写进该模块的 docstring。 |
| R20 | **不要用 `FactorWarehouse` 做只读探针/测试。** 它的 `__init__` 走 `_get_or_create_shared_connections`（`store.py:544`）以 **read_only=False** 打开数仓；文件被其它进程持有时会**一直等锁**——实测挂死 5 分 44 秒、无输出无异常。只读脚本直接用 `duckdb.connect(path, read_only=True)`；测试一律注入现成的数据面板。 |
| R21 | **静态路由必须声明在动态路由之前。** FastAPI 按**声明顺序**匹配：`/candidate-pools/filter-presets` 若排在 `/candidate-pools/{pool_id}` 之后，会被当成 `pool_id="filter-presets"` → 404 且**没有任何告警**。新增端点后必跑顺序断言（见 `_verify_t09_wiring.py`）。 |
| R22 | **引入 `python-multipart` 前先想清楚它是不是「应用级硬依赖」。** FastAPI 的 `File(...)`/`Form(...)` 在**路由注册期**（import 时）就检查它，缺失会抛 `RuntimeError` → **整个后端起不来**，而不只是那个端点不可用。若只是个别端点需要上传，应改为读 `Request` 手工解析并降级为 503（见 `mining_candidate_pool.py::_read_upload`），保留一条零依赖通道。 |
| R23 | **`canonical_json` 会静默丢弃所有 None 值 —— 只能用于哈希，不能用于落库。** 实测 `{'a': None}` → `{}`（0/False/空串保留）。哈希场景 None 与缺失等价，是合理设计；**冻结/快照/审计等「要原样还原」的落库必须用保留 None 的序列化**（见 `service._freeze_json`）。判断口诀：**算哈希用 canonical_json，存数据用 _freeze_json**。 |
| R24 | **SQLite 会静默忽略 `SELECT ... FOR UPDATE`** —— 测试环境与生产（MySQL InnoDB）的并发语义不同。凡是「读→改→写回」的临界段（如 task_lock 的 queue_json），在测试里必须补进程内锁（`task_lock._QUEUE_LOCK`），否则并发测试会复现生产上不存在的竞态、或掩盖真实缺陷。判断口诀：**FOR UPDATE 在 SQLite 是摆设**。 |
| R25 | **worker 线程的提交对测试 fixture session 不可见**（SQLite 快照读 + 独立 session）。轮询终态前必须 `db_session.expire_all()`；teardown 里 `request_all_workers_stop()` 后必须 `WORKER_STOP_EVENT.clear()`（模块级全局，不清会毒化后续测试）。判断口诀：**跨线程读库，先 expire**。 |
| R26 | **新建 worker 类型必须同时响应两个停止信号**：全局 `WORKER_STOP_EVENT` （`is_worker_stop_requested`）**和** `async_task_records.cancel_requested`。`cancel_async_task` 对 running 任务只置后者（既有语义，不改）—— 只查前者的 worker 会让用户「暂停/取消」永远停不下来（T14 真实踩中）。分片边界处 `expire_all` 后重读标志位。 |
| R27 | **「断点续跑」类任务的 resume 必须迁移旧进度 + 有端到端测试**。「同 payload 新建任务」的 resume 若不把旧 `batch_recovery_json` 复制过去，会从零重跑、重复执行已完成单元 —— 且只靠手工构造 partial recovery 的单测**发现不了**（T14 漏、T15 端到端才暴露）。resume 后先迁移进度，再启动 worker。 |
| R28 | **同一逻辑只能有一处实现，并加 parity 测试钉死。** 配置哈希这类「多处复用同一算法」的东西，两处实现哪怕今天一致，任一侧调整就会静默分叉（表现为幂等失效、重复建任务，**不报错**）。T16 的 `test_parity_with_validation_service` 逐例比对两处实现 —— 它当场抓出了「列表内 None 该不该删」的分歧。**凡是「同一算法两处实现」，必须 parity 测试。** |
| R29 | **给函数加了「可注入」参数，就要真的用它。** T17 的 `validate_candidates(..., compiler=None)` 声明了参数却在函数体内直接 `from ... import compile_formula` —— 参数被静默忽略，导致配额类测试无法确定性运行（真编译慢且依赖字段注册表）。规则：**缺省值分支才 import**，否则一律用注入的实现。同类坑：声明了 `warehouse` 却内部 `_default_warehouse()`。 |
| R30 | **按名字引用另一个模块的注册表（算子/字段/表名）时，必须加「逐名存在性」测试。** T18 的算子签名表若有一个名字在 `factor_compiler.FUNCTION_CATALOG` 里不存在，生成的公式会**全量编译失败，而生成器自己不报错**（只表现为失败计数飙升、产出 0）—— 静默且难查。规则：`[n for n in 本地表 if n not in 对方注册表] == []` 写成断言。 |
| R31 | **判「两个东西是否相同」时，先列出必要维度，再选相似度函数。** T19 实测：「结构相同」至少需要 ①算子集合 ②字段集合 ③**保序骨架（树形）**三维；只比 ①② 会把 `mean(a)/mean(b)-1` 与 `(mean(a)-mean(b))/b` 判为同结构（相似度 1.0）→ **误杀真实不同的因子**。另外：`A 与 B 只差一个符号`时**不要用加权平均**（会把 0.888 与 1.0 平均到 0.93 越过阈值），用 **min**（任一维度不像就不算像）。 |
| R32 | **「渲染」与「规范化」是两件事，不要合并。** 渲染器（`render_ast`）的职责是**无损还原**（宁可多加防御性括号，保证嵌套正确）；规范化（`dedup.canonicalize_formula`）的职责是**唯一化**（去冗余括号、交换律、数字归一）。把去括号塞进渲染器会让「渲染」不再无损，之后任何基于渲染的断言都失去意义。同理：**「报事实」的函数（`value_quality`）不要夹带阈值判定**——阈值属于配置。 |
| R33 | **数值列要表达「无限/不可比较」时，用哨兵值，且哨兵必须落在该列**真实列型**的范围内。** ① `inf` 发不出去（PyMySQL 客户端直接抛 `ProgrammingError: inf can not be used with MySQL`）；② 换 `1e308` 也不行 —— `Float` 在 MySQL 是 **`float` 单精度（上限 ≈3.4028e38）**，不是 `double`，`1e308` 触发 `1264 Out of range` → 整条 UPDATE 失败。哨兵取 **`3e38`**（本项目 `performance_probe.INCOMPARABLE_DIFF`）。另注：通用的「NaN/Inf → 0」归一化会**抹平**哨兵语义，特殊列要显式绕开。 |
| R34 | **SQLite 单测对「数值列范围」天然放行 —— 范围类约束必须按列型核对 + 真实库试探。** SQLite 的 `REAL` 是 8 字节 DOUBLE **且不做范围检查**（`inf` / `1e308` / `1e39` 全能写）；MySQL 的 `Float`（无 `precision`）映射为 **`float` 单精度 ≈3.4e38**。故「单测全绿」**不能证明**写入安全。做法：① 断言哨兵 ≤ 列型上界；② 写入前用 `clamp_*` 兜底；③ 在真实库做一次插入/更新试探（同列型临时表或真实列 + 回滚）。 |
| R35 | **夹取上界不能用「四舍五入后的真实上限」—— 要留余量。** MySQL `FLOAT` 真实上限是 `3.402823466e38`，取 `3.4028235e38` 看似等价，**夹到它反而越界**（实测 `1264`）。故本项目常量取 `3.4e38` / `1.7e308`（`app/core/db_numeric.py`）。推论：**凡是「把值夹到边界」的归一化，边界值本身必须实测可写**，否则归一化工具会在边界处制造新的写库失败。 |
| R36 | **同步函数进异步世界，必须走线程池；且并发上限要用「可观测的行为证据」验证。** 同步 I/O 函数（如 `call_llm_with_failover`）直接 `await` 会阻塞事件循环，让「并发 N 路」静默退化成串行。做法：显式 `ThreadPoolExecutor(N)` + `loop.run_in_executor`（语义等同 `asyncio.to_thread`，但上限确定、生命周期可控）；测试断言「调用发生在线程名前缀 `xxx*`、非主线程」+「同时进行的调用数 ≤ N 且 > 1」。 |
| R37 | **「三率配额」记录分配名额，不记实际产出条数。** 随机供给缺位时其它算子会兜底补齐名额 —— 若按「实际某类操作条数」记账，配额之和就对不上账本。推论：**统计口径必须在写入侧定义一次**（记分配），不要让读侧去反推。 |
| R38 | **「一次性窗口」的锁要做在两层：个体幂等键 + 流程状态机。** test 段评估只许一次：个体级（已有 `factor_version_id` → 拒）防重复评估，流程级（run 状态 → `validating`/`succeeded` 后抛 `MINING_TEST_LOCKED`）防重复进入。且状态机要**单向**（`succeeded` 拒绝一切迁移）。只做一层，另一层就会成为绕过点。 |
| R39 | **批量操作的幂等层级要显式声明**：同一业务对象重复操作时，哪一层幂等（Factor 复用 / Version 新建 / 成员跳过）必须写进契约并在测试里钉住 —— 「code 冲突复用 Factor、内容不同新建 Version」就是 T25 定下的层级语义。 |
---

## 2. 接续协议（多 Agent 协作的核心）

### 2.1 状态账本：`.workbuddy/mining/PROGRESS.json`

**这是唯一的进度事实来源。** 任何 Agent 开工前先读它，收工后必须写它。

```json
{
  "schema_version": "1.0",
  "updated_at": "2026-09-16T10:00:00+08:00",
  "updated_by": "agent-<任意标识>",
  "tasks": {
    "T01": {
      "status": "done",
      "started_at": "2026-09-16T09:00:00+08:00",
      "finished_at": "2026-09-16T09:40:00+08:00",
      "agent": "agent-a",
      "evidence": [
        "cmd: .venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_evaluation_adapter_split.py -q",
        "exit: 0",
        "summary: 6 passed"
      ],
      "artifacts": ["app/services/factors/mining/evaluation_adapter.py"],
      "decisions": ["purge_points 换算表按 REBALANCE_INTERVAL_DAYS={daily:1,weekly:5,monthly:21} 实现，未新增频率"],
      "notes": "月频实测 test=6 点，已同步进 SD-v2.0 §7.2.3"
    },
    "T03": { "status": "in_progress", "agent": "agent-b", "started_at": "..." },
    "T05": { "status": "blocked", "blocked_reason": "T03 未完成，cs_rank 未注册", "needs": "T03" },
    "T07": { "status": "pending" }
  }
}
```

**status 取值**：`pending` / `in_progress` / `done` / `blocked` / `skipped`

> ⚠️ `decisions` 字段是接续的关键：**上一个 Agent 做过的非显然取舍必须写在这里**。
> 下一个 Agent 读到后会知道"为什么这么做"，而不是自己重新想一遍（通常想得不一样 → 漂移）。

### 2.2 任务卡 8 字段

每个任务（§5 主表 + 重点卡）固定包含：

| 字段 | 含义 | 接续作用 |
|------|------|---------|
| `ID` | `T01`~`T40` | 账本索引键 |
| `目标` | 一句话，动词开头 | 防止范围蔓延 |
| `前置` | 必须已 `done` 的任务 ID | 依赖图 |
| `产物` | 本任务**新建/修改**的文件清单 | 也是**写权限声明**（§2.3） |
| `DoD` | **可执行命令 + 期望输出** | 客观完成判定，替代"看起来对了" |
| `读` | 允许读的文件白名单 | 控制上下文预算 |
| `不做` | 明确排除项 | 防越权、防过度实现 |
| `预算` | `S`(<1h) / `M`(1~3h) / `L`(>3h，**应拆分**) | 粒度控制 |

### 2.3 写权限互斥表（**多 Agent 并行唯一的硬约束**）

两个 Agent 同时写同一个文件 = 上下文互相污染，几乎必然冲突。规则：

1. **同一时刻，一个文件只能被一个 `in_progress` 任务声明为产物。**
2. `next_task.py` 会**自动排除**与当前 `in_progress` 任务产物冲突的候选任务。
3. 如果确实需要改别人正在写的文件 → **不要改**，写进 `PROGRESS.json` 的 `blocked_reason`，等对方 `done`。

**本项目的高冲突文件（必须串行）**：

| 文件 | 相关任务 | 串行要求 |
|------|---------|---------|
| `app/services/factors/factor_compiler.py` | T03, T04, T05, T06 | **完全串行**，还涉及 FIELD_CATALOG/FUNCTION_CATALOG/白名单**四处**联动（见 T03 卡） |
| `app/services/factors/formula_catalog.py` | T03, T04, T06 | **串行**；`_FUNCTION_META` 与 `FUNCTION_CATALOG` 必须逐键对齐（硬索引，漏键 KeyError） |
| `app/services/factors/factor_executor.py` | T05 | 与 T03/T04 串行 |
| `app/services/factors/factor_evaluator.py` | **任何任务都不得修改**（R2） | 只读 |
| **`app/api/router.py`** | **T01, T07, T14, T15, T26** | **串行**。每个新增路由文件都要来这里加一行注册——这是最容易被忽视的冲突源（已由 `_selfcheck_conflict.py` 场景 C3 覆盖） |
| `alembic/versions/` | T01, T07, T26, T36 | `down_revision` 链必须串行，**不可并行新建修订**（场景 C5） |
| `app/services/factors/mining/service.py` | T23, T24, T25 | **串行**（场景 B：T23 vs T25 冲突已断言） |
| `requirements.txt` | T01, T35 | 串行 |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | T27, T32, T37, T40 | **必须两份同步**；串行（场景 C6） |
| `frontend/src/components/Settings.tsx` | T27, T39 | **只允许一个任务改** |
| `app/models/__init__.py` | 无需修改（有 `_auto_discover_models`） | — |

> 上表的机器可读版本在 `tasks.json` 的 `high_conflict_files`。**它同时是 `next_task.py` 判定冲突的兜底依据**——因为 `alembic/versions/0054_*.py` 与 `0055_*.py` 在路径上不重叠，只有目录级的声明能拦住它们。

### 2.4 交接仪式

**开工前 4 步**：
1. 跑 `next_task.py` 拿任务 ID（不要自己挑"最喜欢的"）
2. 读 `PROGRESS.json` 中该任务的前置任务条目，**特别读它们的 `decisions` 和 `notes`**
3. 读任务卡的 `读` 白名单
4. 把该任务置为 `in_progress` 并写 `agent` 标识（**先占位，再动手**——防止两个 Agent 抢同一任务）

**收工前 5 步**：
1. 跑完 `DoD` 全部命令，记录 `exit` 与摘要
2. 确认产物文件确实存在且被写（用 Read 抽查，不要只凭记忆）
3. 更新 `PROGRESS.json`：`status=done` + `evidence` + `decisions` + `artifacts`
4. **刷新进度看板**（一条命令，别手工改那份 md）：
   ```bash
   .venv/Scripts/python.exe .workbuddy/mining/update_progress_doc.py
   ```
5. **不要顺手改其他文件**。发现别处有问题 → 记到 `PROGRESS.json` 的 `observations`，不自行修
6. **自查回归覆盖**（R18）：按 §1.4 的自查表，把你**改过的每个源文件**对应的
   `test_whitebox_*.py` / `tests/factors/` 都跑一遍，不能只跑本轮新增测试

> **若本任务新增了 alembic 迁移**（T01/T07/T26/T36），收工仪式额外加一步：
> 跑 `verify_schema_drift.py` 确认 `无漂移`，并把输出贴进 `evidence`。
> 不跑就等于没验收 —— 因为 `_auto_align_all_schema` 会把迁移缺陷静默补上，
> 你看不出问题（R13）。

> 进度看板 `docs/因子挖掘-开发进度看板.md` 是**由 `PROGRESS.json` 自动生成**的。
> 不要在它上面手工填进度——那样它和账本会变成两个事实来源，然后必然不一致（多 Agent 最典型的翻车方式）。
> **只改账本，再跑脚本。**

### 2.5 阻塞升级（"不许自行发挥"清单）

遇到下列情况**必须停手并标 `blocked`**，不允许自己想办法绕：

| 情况 | 为什么不能绕 |
|------|-------------|
| 前置任务未 `done` | 依赖的接口/表可能不存在，绕过去就是幻影代码 |
| 冻结契约（§3）看起来"不对" | 你觉得不对很可能是你没看到上游 `decisions`；改契约会连锁破坏已完成任务 |
| DoD 命令跑不通且不是自己代码的错 | 可能是环境问题（§1.1）或上游缺陷，需要人来判 |
| 需要新增第三方依赖 | 影响全项目环境，必须人工批准 |
| 需要修改 R2 列出的 L1 文件签名 | 会破坏因子中心既有链路 |
| 发现需求文档与实现矛盾 | 以 SD-v2.0 为准；SD-v2.0 也没覆盖 → 提问，不要自己定 |

**升级写法**：

```json
"T19": {
  "status": "blocked",
  "blocked_reason": "classify_category 的归类优先级在 SD-v2.0 §7.7 给了顺序，但未给出'含财报字段且含估值字段'时的具体判据",
  "needs": "人工裁决：是否按 CATEGORY_PRIORITY 元组顺序取第一个命中项",
  "attempted": "已尝试按 CATEGORY_PRIORITY 顺序实现，但不确定边界用例",
  "partial_artifacts": ["app/services/factors/mining/category.py"]
}
```

---

## 3. 冻结契约（Frozen —— 改了就是 bug）

> 本节内容在 T01 完成后冻结。**任何任务不得重新定义**。新增只能"加可选字段"。

### 3.1 常量表（**必须逐字一致**）

```python
# app/services/factors/mining/evaluation_adapter.py
REBALANCE_INTERVAL_DAYS = {"daily": 1, "weekly": 5, "monthly": 21}

# (min_total_points, min_val_points, min_test_points)
SPLIT_MINIMUMS = {
    "daily":   (252, 100, 50),
    "weekly":  (104,  20, 10),
    "monthly": ( 36,   6,  3),
}

DEGRADED_TEST_THRESHOLD = 24      # test < 24 点 → 不做 Bootstrap/置换，最高 B 级
SPLIT_ALGORITHM_VERSION = "split-1.0.0"

# app/services/factors/mining/task_lock.py
LOCK_MINING_DOMAIN = "mining_domain"
LOCK_DUCKDB_WRITE  = "duckdb_write"
HEARTBEAT_TIMEOUT  = 1800          # 秒

# app/services/factors/mining/contracts.py
CATEGORY_PRIORITY = (
    "quality", "valuation", "volatility", "volume_price", "reversal", "trend",
)

# 繁殖安全护栏（C1 不得突破）
MUTATION_RANGE  = (0.40, 0.80)
CROSSOVER_RANGE = (0.10, 0.50)
RANDOM_RANGE    = (0.05, 0.20)
HYSTERESIS_GENERATIONS = 2
MAX_STEP = 0.10

# AI 生成预算
AI_MAX_CALLS = 10
AI_CALL_TIMEOUT_SEC = 30
AI_TOTAL_TIMEOUT_SEC = 300
AI_MAX_TOKENS = 500_000
AI_MAX_COST_YUAN = 10
```

**换算函数（唯一实现路径）**：

```python
def normalize_purge_points(*, target_horizon_days: int, frequency: str) -> int:
    return max(1, math.ceil(target_horizon_days / REBALANCE_INTERVAL_DAYS[frequency]))
# daily→5  weekly→1  monthly→1   (target_horizon=5)
```

> ⚠️ **为什么这条是 P0**：`build_time_split` 的 `purge_days/embargo_days` 是**传入 `all_dates` 的索引间隔**（`factor_evaluator.py:268`）。挖掘传的是「有效调仓点」，直接传 `5` 会让月频"隔离 5 个月"。**同时必须显式传 `min_validation_days`**，否则默认 50 会推出 `derived_min_total=260`，周频 243 点 / 月频 60 点全部抛 `insufficient_dates`。**两处必须同时改，只改一处仍然失败。**

### 3.2 表结构（15 项 / 22 张实体表）

| 域 | 表 |
|----|----|
| 候选池 | `training_candidate_pools` / `_members` / `_snapshots` |
| 挖掘 | `factor_mining_runs` / `_candidates` / `_generations` / `_prescreen_fingerprints` / `_drafts` / `_templates` / `_template_versions` / `factor_training_checkpoints` / `factor_data_validation_runs` |
| F1 | `factor_experience` / `_tags` / `_metrics` / `_field_deps` |
| 平台 | `task_locks` / `factor_formula_templates` / `factor_grade_history` |
| 复用表增量 | `factor_evaluation_runs` +2 列（`test_start_date`/`test_end_date`）；`factor_versions` +4 列（M2） |

**必须存在的唯一约束**：`(run_id, formula_hash)`、`(run_id, generation)`、`(pool_id, symbol_id)`、`task_locks.lock_key`、`factor_experience.fingerprint`。

**alembic 基线**：head = `wps_0023_050_factor_model_members`；新修订从 **0053** 起，命名 `YYYY_MM_DD_NNNN_wps_0023_NNN_<slug>.py`。**修订必须幂等**（`sa.inspect().has_table` 短路）。

### 3.3 服务层签名（见 SD-v2.0 §8.7，不得改名/改参序）

`run_evolution` / `run_generation` / `select_elites` / `build_split` / `compute_split_budget` / `evaluate_short` / `evaluate_full` / `assert_no_leakage` / `acquire_mining_lock` / `acquire_write_slot` / `release_lock` / `heartbeat` / `get_lock_status` / `SubexprCache.*` / `run_statistical_suite` / `grade` / `store_experience` / `sample_experiences`

### 3.4 前端 TS 类型（`frontend/src/types/mining.ts`）

`MiningRunStatus` / `RebalanceFrequency` / `FactorCategory` / `OperationType` / `QualityGrade` / `LogicSource` / `SplitBudget` / `LockStatus` / `GenerationStat` / `CandidateRead` / `LineageNode` / `StatResult` / `GradeEvidence` / `PoolAnalysis` / `FieldValidationReport`

### 3.5 错误契约（**与既有代码对齐，不是文档里的 `fix_action`**）

后端：`app/schemas/errors.py::FactorSevenError`，输出
`error_code / title_zh / detail_zh / correlation_id / impact / fix_link / retryable` + `extras`。

结构化修复动作放 **`extras.fix_action = {action, target_step, message}`**（不是顶层字段——顶层没有 `fix_action`）。

新增错误码（加入 `ERROR_CODE_LIBRARY`）：
`MINING_DOMAIN_BUSY` / `MINING_QUEUE_FULL` / `MINING_SAMPLE_INSUFFICIENT` / `MINING_POOL_TOO_SMALL` / `MINING_VALIDATION_EXPIRED` / `MINING_AI_BUDGET_EXCEEDED` / `MINING_TEST_LOCKED`

---

### 3.6 候选池筛选：字段可用性基线（**T09 实测，2026-09-16**）

> 这一节的每条结论都来自对真实 MySQL + 6.2GB DuckDB 的查询，**不是**从需求文档推断的。
> 动手做候选池相关功能前先读它，能省掉「按需求写完发现字段全空」的一整轮返工。

| 向导要求的筛选条件 | 权威来源 | 实测 | 结论 |
|---|---|---|---|
| A 股 universe | `universe_symbols`（asset_type=stock, region=cn, is_synced=1） | 5,554 | ✅ |
| 板块（沪主板/深主板/创业板/科创板/北交所） | `universe_symbols.market`+`.board` | sh_main 1700 / sz_main 1498 / gem 1403 / star 615 / bj 339 | ✅ **只能用它** |
| ST / 退市风险 | `universe_symbols.name` 模式匹配 | `is_st` 列**全为 0**；`security_status_daily` 是 **e2e fixture 残留** | ⚠️ 仅「当前名称」语义 |
| 停牌 / 指数成分 | — | 无字段、无事件表 | ❌ 阻断 |
| 上市满 N 交易日 | `symbols.listed_at` / `universe_symbols.listed_at` | **两边均 0 行非空** | ❌ 阻断 |
| 总市值 / 流通市值 | DuckDB `raw_valuation_snapshots` | 100% 非空正值；但**按日覆盖波动极大**（8 月 ~5,544 / 9 月 ~1,904） | ✅（需报告覆盖面） |
| PE_TTM | 同上 | 100% 非空，**80.9% 正值** | ✅ |
| PB | 同上 | 100% 非空，99.6% 正值 | ✅ |
| 股息率 | 同上 | **0 行非空（全 NULL）** | ❌ 阻断 |
| 流动性（日均成交额/量/换手率） | DuckDB `raw_daily_bars` | 100% 非空；**仅 401 个交易日**（2025-01-13 起） | ✅（窗口需回退并报告实际天数） |
| 导入「已停牌」判定 | `symbols.is_active` | `=0` 在 A 股 stock 中仅 **7 只**、名称全为「退市XX」→ 真实语义是**已退市/不可交易** | ⚠️ **无真正的停牌字段** |
| ROE_TTM | DuckDB `raw_financial_reports`（PIT: `announcement_date <= as_of`） | 83.2% 非空；2026-08-21 截面仅 **20~31%** 可得 | ✅（低覆盖会告警） |
| 净利润 / 净利润同比（近三年亏损） | 同上 | **均 0 行非空** | ❌ 阻断 |
| 行业 | `symbols.industry` / `universe_symbols.industry` | **两边均 0 行非空** | ❌ 阻断 |

**两条由此推导出的硬规则**

1. **`as_of_date` 绝不能取 `MAX(trade_date)`** —— 实测 `raw_daily_bars` 最新日 2026-09-07
   **只有 1 个标的**。必须按「被引用数据源在该日有完整截面」判定
   （`rules._resolve_as_of_date`：估值截面标的数 ≥ 近 20 日中位数 × 0.9），
   并把 `candidate_ratios` 等证据一并返回。
2. **成员 `symbol_id` 是 `symbols.id`，筛选 universe 是 `universe_symbols` —— 两套自增主键**。
   必须按 `symbol` 代码映射（实测 A 股 5,554 只 **100%** 命中，两表 `symbol` 列都唯一）。

**T10 依赖变更（已按 R3 声明，需人工确认）**
- 新增 `python-multipart>=0.0.9`（HTTP 文件上传必需；FastAPI 的 `File(...)` 在
  import 期检查它，缺失会让**整个后端起不来**）
- 补声明 `xlrd>=2.0`（读旧版 .xls；2.x 仅支持 .xls）
- 上传端点刻意不用 `UploadFile = File(...)`，改为读 `Request` 手工解析，
  缺依赖时返回 503 而不是拖垮应用；同时保留「body 直接放文件 + `X-Filename`」
  的零依赖通道

**数据卫生问题（已上报，未修）**
- `security_status_daily` 全部来自 e2e fixture（`T26_FIXTURE` / `T30_FIXTURE`），不可当真实数据源。
- `symbols` 与 `universe_symbols` 的 asset_type/market/board **冲突 2,333 行**
  （如 `920305`：`universe_symbols`=bj、`symbols`=sh）。

### 3.7 已裁决项（2026-09-16 需求方拍板 —— **不要再重开讨论**）

> 完整版见 SD-v2.0 §12.3。**这 4 项已定，不是「建议」**。若你认为需要改动，
> **必须先提出新证据并走一次裁决**，不得以「看起来更合理」为由直接改代码。

| # | 议题 | **裁决结果** | Agent 必须遵守 |
|---|------|------------|---------------|
| **D-A** | 单标的沙箱（`indicator_ast_sandbox`，被 `discovery_fast_scan` 高级筛选使用）对截面算子 `cs_*` 的处理 | **登记但拒绝** | `cs_*` 名进 `ALLOWED_FUNCS` 以便自省，但 `_validate()` 在构造期抛 `ValueError` 并说明「需要面板上下文」。**禁止**改成静默返回 None；**禁止**为此给沙箱引入面板上下文（超 M1 范围） |
| **D-B** | 「AST 节点标注计算方向」是否落库进 `ExecutionPlan` | **不落库** | 方向只作运行时查询（`FUNCTION_DIRECTION` + `collect_directional_calls`）。**禁止**写进 `to_canonical_json()` / `formula_ast` —— 会让**全量**既有 `factor_versions.execution_plan_hash` 失效、同公式重复建版本 |
| **D-C** | 月频 S/A 通道门槛（原 Q3） | **月频永久最高 B 级** | 写入产品文案与设置页说明；日/周频 `test ≥24` 不动；**代码禁止写死 24**，做成可配置项 |
| ~~**D-D**~~ | ~~`prev_close` 虚注册~~ → **❌ 已撤销（2026-09-16 T06 执行时实测推翻）** | **保留 `prev_close`，不做任何改动** | **禁止删除 `prev_close`。** 详见下方「D-D 撤销」与 SD-v2.0 §12.3 |

> **D-D 撤销（必读 —— 防止错误结论被再次执行）**
>
> 原判据写的是「`app/services/factors/` 内**无任何派生实现**」——**该结论来自一次被截断的搜索**
> （`Grep` 当时带 `head_limit`，`factor_executor.py` 的命中落在截断之外）。**这是方法错误，不是代码事实。**
>
> 实测物证：
> 1. `FactorExecutor._select_field_expressions`（`factor_executor.py:674-685`）把 `prev_close` 投影为
>    `LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close`，两个取数点
>    （`:891` / `:1352`）都走这条投影 —— 它是**可用的虚字段**；
> 2. 真实数仓端到端实测：`000001` 在 `2026-07-21..24` 的 `prev_close` = `10.98 / 10.84 / 10.98 / 11.08`，
>    等于上一交易日收盘价；
> 3. 既有**绿灯**测试 `test_preview_derives_prev_close_without_physical_column` 与
>    `tests/factors/test_preflight_api.py` 都断言它是可用派生字段。
>
> **若执行原裁决会造成**：打破 2 个既有绿灯测试、移除一项**正在工作**的能力、破坏 DSL-001
> （`close / prev_close - 1`）与前端 `AiChatDrawer` 字段词表。
>
> 已加 **5 条保活哨兵** 进 `tests/services/factors/mining/test_template_compile_rate.py`
> （含「执行器必须把它投影成 LAG SQL」这条直接断言）。

---


#### 2026-09-17 补充裁决（D-E~D-H）

| 编号 | 裁决 | 影响面 |
|---|---|---|
| **D-E** | 去重第 3 层改为**同来源内**聚类（`similarity_scope="same_source"`，新增参数默认值）。理由：同一模板的参数变体是**参数族**不是重复，GA 要靠它们调参；跨来源同一因子由第 2 层管、高相关近似由第 4 层管 | `dedup.py` |
| **D-F** | 挖掘域审计 JSON **保留 None**（`_audit_json`，弃用 `canonical_json`）。理由：审计要能还原当时状态，「None」与「未记录」不可混为一谈 | `candidate_pool/service.py::_write_audit` |
| **D-G** | 纯估值/财报公式**仍须日线 spine**，缺则给**结构化可操作报错**（保留 `no_source_data` 前缀，不改成抛异常）。据此**两个既有红灯转绿** | `factor_executor.py::_no_spine_error` |
| **D-H** | 审计加**独立列 `pool_id` + 索引**（迁移 0055），`pool_id IS NOT NULL` ⇔ 挖掘域审计；历史行不回填 | `models/factor_runtime.py` + 迁移 0055 |



## 4. 排期总览（8 阶段 / 40 任务）

### 4.1 阶段划分与门禁

| 阶段 | 名称 | 任务 | 出口门禁 | 门禁含义 |
|------|------|------|---------|---------|
| **PH0** | 校准与哨兵 | T01~T02 | **G0** | 骨架就位 + 切分归一化单测绿 |
| **PH1** | DSL 与数据地基 | T03~T06 | **G1** | 25 模板编译率 ≥90% |
| **PH2** | 候选池域 | T07~T11 | **G2** | 能冻结一个带看板的候选池快照 |
| **PH3** | 任务与锁 | T12~T15 | **G3** | 双锁并发穿透测试绿 + 镜像可续跑 |
| **PH4** | 进化闭环 | T16~T26 | **G4** | 端到端跑通 20 代并产出候选因子 |
| **PH5** | 前端骨架 | T27~T32 | **G5** | 5 步向导可点通到结果页 |
| **PH6** | M2 算法 | T33~T37 | **G6** | 选择/繁殖/统计/分级全链路端到端 |
| **PH7** | M3 收尾 | T38~T40 | **G7** | G1 决策 + en-US 补齐 |

> **门禁未过不得进入下一阶段。** 门禁是可执行检查，不是评审会（见 §6）。

### 4.2 依赖图（→ 表示"必须在之前完成"）

```text
T01 ─┬─► T02 ─┬─► T03 ─► T04 ─► T05 ─► T06 [G1]
     │        │
     │        └─► T07 ─► T08 ─► T09 ─► T10 ─► T11 [G2]
     │                  │
     │                  └─► T12 ─► T13 ─┬─► T14 [G3]
     │                                 └─► T15
     │
     └─► T16 ─► T17 ─► T18 ─► T19 ─┐
                    │              │
                    ├─► T20 ─► T21 ┤
                    └─► T22 ───────┤
                                   └─► T23 ─► T24 ─► T25 [G4] ─► T26
                                                          │
T27 ─┬─► T28 ─► T29 ─► T30 ─► T31 ─► T32 [G5] ◄────────────┘
     │
     └─► (T27 无上游依赖，可与 PH1 并行)

T33 ─► T34 ─► T35 ─► T36 ─► T37 [G6]     (依赖 T26)
T38 ─► T39 ─► T40 [G7]                   (依赖 G4)
```

### 4.3 并行批次（**已按写权限互斥排好**）

| 批次 | 可并行的任务 | 说明 |
|------|-------------|------|
| B1 | T01 | 单任务，不可并行（契约冻结是串行瓶颈） |
| B2 | T02 ‖ T27 | T02 动后端、T27 动前端，无冲突 |
| B3 | T03 ‖ T07 ‖ T27续 | 三处文件不重叠 |
| B4 | T04 ‖ T08 ‖ T28 | — |
| B5 | T05 ‖ T09 ‖ T12 ‖ T29 | — |
| B6 | T06 ‖ T10 ‖ T13 ‖ T30 | — |
| B7 | T11 ‖ T14 ‖ T17 ‖ T31 | — |
| B8 | T15 ‖ T16 ‖ T18 ‖ T32 | — |
| B9 | T19 ‖ T15续 | — |
| B10 | T20 ‖ T22 ‖ T33 | T20 缓存 / T22 AI / T33 选择，互不重叠 |
| B11 | T21 ‖ T23 ‖ T34 | — |
| B12 | T24 ‖ T35 | — |
| B13 | T25 ‖ T36 | — |
| B14 | T26 ‖ T37 ‖ T38 | T26 路由/前端、T37 前端、T38 后端 |
| B15 | T39 ‖ T40 | — |

> **每个批次最多 3~4 个并行 Agent**。超过后 `PROGRESS.json` 的合并冲突概率显著上升，收益为负。

### 4.4 粒度纪律

- 单任务预算 **≤ `L`（3 小时）**。超过就必须拆分，拆法：按"端到端可验证的最小切片"拆，**不是**按代码层拆。
- 单任务产物文件数 **≤ 8 个**。超过说明任务太大。
- 单任务 `DoD` 命令数 **1~3 条**。超过说明验收面太宽，该拆。

**唯一的粒度豁免（须显式声明理由）**：T01 有 13 个产物，超出上限。理由是它 **90% 是"机械搬运已写好的骨架文件"**，决策面极小，拆开只会增加交接成本。粒度纪律防的是**决策面过宽**，不是文件数本身。

豁免必须在 `tasks.json` 里写 `granularity_exempt: true` + `granularity_note`，且 `_selfcheck_conflict.py` 会校验理由非空。**豁免条件：产物必须原样搬运，不得在搬运中改设计。**

> 这条豁免是自检脚本抓出来的——`_selfcheck_conflict.py` 的 C12 会拒绝"未声明豁免却超限"的任务。不要为了让它变绿而直接放宽阈值。

---

## 5. 任务卡

### 5.1 主表（40 任务）

> `写` = 本任务允许写的文件（也是写权限声明）。未列出的文件**一律不许改**。

#### PH0 校准与哨兵

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T01** | 骨架搬入 + alembic 迁移跑通 + 契约冻结 | — | 见重点卡 | 见重点卡（**scratch 库**上 upgrade / downgrade -1 / upgrade 全 exit 0） | M |
| **T02** | 切分归一化哨兵单测（防泄漏） | T01 | `tests/services/factors/mining/test_evaluation_adapter_split.py` | `pytest ... -q` exit 0，≥8 passed | M |

#### PH1 DSL 与数据地基

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T03** | `cs_` 截面算子族 + 注册双写 | T02 | `factor_compiler.py`、`indicator_ast_sandbox.py`、`dsl/cross_section.py` | `cs_rank(A)-cs_rank(B)` 编译通过；沙箱不拒 | M |
| **T04** | `ts_delta_bars`/`ts_delta_periods`/`ts_atr` | T03 | 同上 + `dsl/time_series.py` | 两算子单位差异断言（**91 倍日历跨度**）；`ts_atr` = **close-to-close**（`ref(close,1)` 派生 prev_close），**只依赖传入序列**；沙箱必须给**真实现** | M |
| **T05** | 方言归一 + `factor_executor` 截面/时序分支 | T04 | `dsl/dialect.py`、`factor_executor.py`、**`factor_compiler.py`**（原卡漏写） | 方言形态与规范形态必须**同 hash**（否则因子库出孪生条目）；归一**仅在公式内**（后处理不动）；`delta` 判不出来要**报错不猜**；executor 加**两个**分支 + Series↔面板适配 | M |
| **T06** ✅ | 字段注册 **+3**（`prev_close` **保留**）+ 25 模板编译率 | T05 | `factor_compiler.py`、`formula_catalog.py`、`tests/.../test_template_compile_rate.py` | **编译率 ≥90%**（实测 100%）；`prev_close` 按 **D-D 撤销**保留；`delta` 单位歧义已在 T05 解决 | M |

#### PH2 候选池域

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T07** ✅ | 候选池 3 表验收 + 补 6 个缺失外键（迁移 0054） | T02 | `models/mining_candidate_pool.py`、`alembic/versions/2026_09_16_0054_wps_0023_052_mining_fk_constraints.py` | 迁移双向 exit 0（**scratch 库**）；无漂移（含外键）；外键行为验证通过 | M |
| **T08** ✅ | 候选池 CRUD + 成员软删除 + 审计 | T07 | `candidate_pool/{__init__,service}.py`、`routes/mining_candidate_pool.py`、`app/api/router.py` | 软删除后主数据仍在（`symbols` 行数不变）；写审计（`FactorAuditLog`）；`source_type` 不可切换 | M |
| **T09** | 条件筛选规则编译 + 预览 + 分位数预设 | T08 | `candidate_pool/{rules,presets}.py` | <50 阻断；预览不落库 | L |
| **T10** | CSV/Excel 导入 + 逐行匹配结果 | T08 | `candidate_pool/importer.py` | 模板下载/逐行错误/未匹配不入池 | M |
| **T11** | 看板分析 + 冻结快照 + 锁定/重置 | T09,T10 | `candidate_pool/analysis.py` | 端到端产出 `snapshot_id` + `analysis_json` | L |

#### PH3 任务与锁

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T12** | 双锁 service + 并发穿透测试 | T07 | `mining/task_lock.py`、`models/task_lock.py` | N 线程并发 acquire 仅 1 成功 | M |
| **T13** | `task_runner` 任务入口 + 心跳 | T12 | `mining/task_runner.py` | **断言 `_start_worker` 被调用**；心跳落库 | M |
| **T14** | 字段异步校验（分片 + 续跑） | T13 | `mining/validation_service.py`、`routes/factor_mining_wizard.py` | 同 `config_hash` 复用；续跑不重复分片 | L |
| **T15** | 历史镜像任务 + 状态 API | T12 | `mirror_task.py`、`routes/data_mirror.py` | watermark 续跑不重复行 | M |

#### PH4 进化闭环

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T16** | `config_hash` + 草稿服务 | T13 | `mining/config_hash.py`、`mining/draft_service.py` | 同配置哈希幂等 | S |
| **T17** | `initial_population` 经典底座 | T06,T16 | `mining/initial_population.py`、`mining/category.py` | 均衡覆盖每类有代表；缺口流转 | L |
| **T18** | 受约束随机生成器 | T17 | `mining/random_generator.py` | 复杂度上限；哈希拦截；同结构≤3 变体 | M |
| **T19** | 去重四层（前 3 层） | T18 | `mining/dedup.py` | 交换律归一；相似度 >0.9 归组 | M |
| **T20** | G2 子表达式缓存 | T16 | `mining/subexpr_cache.py` | 断言 SHA-256；`compute_node` 调用=0；无 DuckDB 写 | L |
| **T21** | 性能探针 | T20 | `mining/performance_probe.py` | 9 个探针字段落库 | S |
| **T22** | AI 骨架生成 | T18 | `mining/ai_generator.py` | 缺 `economic_logic` 即丢弃；10 次/5min/¥10 上限 | L |
| **T23** | GA 主循环（朴素） | T19,T20,T21 | `mining/genetic_algorithm.py` | 20 代跑通；精英不变异；val/test 未进适应度 | L |
| **T24** | 最终验证 + 结果落库 | T23 | `mining/service.py` | test 仅评估一次；`total_trials` 累加 | L |
| **T25** | 候选转正式因子 + 批量操作 | T24 | `mining/service.py`、`routes/factor_mining.py` | 走 `create_factor_draft`；frozen 集合被拒 | M |
| **T26** | F1 存回/抽取接口 | T16 | `experience/*`、`models/factor_experience.py`、`routes/factor_experience.py` | 参数泛化；三层指纹；挖掘不 import F1 model | L |

#### PH5 前端骨架

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T27** | Settings 6 处 + `MiningShell` + i18n | — | `Settings.tsx`、`factors/mining/MiningShell.tsx`、`i18n/*.ts` | 菜单可见；`settings:navigate` 跳转生效；i18n 对齐测试绿 | M |
| **T28** | Step1 候选池 UI | T11,T27 | `mining/wizard/step1/*` | 筛选/导入互斥；锁定态置灰 | L |
| **T29** | Step2 时间与目标 UI（含 SplitBudget） | T28 | `mining/wizard/step2/*` | **purge 同时显示调仓点数与交易日数** | M |
| **T30** | Step3 字段与校验 UI | T14,T29 | `mining/wizard/step3/*` | 5s 轮询；阻断只给两个出口 | M |
| **T31** | Step4 进化参数 + 资源确认 | T30 | `mining/wizard/step4/*` | 锁状态三态文案；ETA 标"估算" | L |
| **T32** | Step5 运行跟踪 + 结果页 | T31,T25 | `mining/wizard/step5/*`、`mining/result/*` | 研究声明；跳转携带 4 项上下文 | L |

#### PH6 M2 算法

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T33** | 选择机制 A1/B1/B2 | T23 | `mining/selection/*` | 固定种子可复现；rank 优先同 rank 比 CD | L |
| **T34** | 繁殖自适应 C1/C2/C3/D1/D2/D3 | T33 | `mining/reproduction/*`、`diversity.py`、`convergence.py` | D3 不改率；C1 唯一调参；三率和=1 | L |
| **T35** | 统计层 8 方法 | T24 | `mining/statistical_tests.py` | 与手工小样本一致；月频 `degraded=true` | L |
| **T36** | 质量分级 + 季度重评 | T35 | `mining/factor_grading.py`、`models/factor_grade_history.py` | 五级边界；`decay_ratio<0.5` 不得 B 级以上 | L |
| **T37** | F1 列表页 + 等级标签 + 证据抽屉 | T26,T32 | `mining/config/MiningExperiencePage.tsx`、`mining/result/*` | 按等级筛选；D 级默认不勾选 | M |

#### PH7 M3 收尾

| ID | 目标 | 前置 | 写 | DoD 摘要 | 预算 |
|----|------|------|-----|---------|------|
| **T38** | G1 并行决策与实施 | T21 | `mining/parallel.py` 或**决策文档** | 依据探针数据给出"做/不做/做哪段" | L |
| **T39** | 拆分 `Settings.tsx` | T27 | `Settings.tsx` 及其拆分产物 | 拆分后行为不变，既有测试全绿 | M |
| **T40** | en-US 补齐 | T32 | `i18n/en-US.ts` | `translations.test.ts` 绿 | M |

### 5.2 重点任务卡（高风险 / 有坑，逐字执行）

#### T01 · 骨架搬入 + 迁移 + 契约冻结 — 预算 M

| 项 | 内容 |
|---|---|
| **目标** | 把 SD-v2.0 附录 A 的骨架从 staging 搬入仓库，跑通迁移，冻结 §3 契约 |
| **前置** | 无 |
| **写** | `app/models/{factor_mining,mining_candidate_pool,task_lock}.py`、`app/services/factors/mining/{contracts,evaluation_adapter,task_lock}.py`、`app/services/factors/dsl/cross_section.py`、`app/api/routes/factor_mining.py`、`alembic/versions/2026_09_16_0053_*.py`、`app/api/router.py`、`alembic/env.py`、`requirements.txt`、`app/schemas/errors.py` |
| **读** | SD-v2.0 §5、§8.7；`docs/factor-mining-v2-skeleton/README.md` |
| **不做** | 不实现任何 `NotImplementedError` 的业务逻辑；不动 L1 既有文件（除 `router.py`/`errors.py` 的两处追加） |
| **DoD** | 全部在**一次性 scratch 库**上执行（见下方「坑」第 1 条）：<br>① `ALEMBIC_DATABASE_URL=sqlite:///<scratch>.db {PY} -m alembic upgrade head` → exit 0<br>② 同上 `-m alembic downgrade -1` → exit 0<br>③ 同上 `-m alembic upgrade head` → exit 0（幂等二次通过）<br>④ 同上 `downgrade -1 && upgrade head` → exit 0（双向可逆）<br>⑤ `{PY} -m alembic heads` → `wps_0023_051_factor_mining_core` |
| **坑** | 以下 7 条均为 **2026-09-16 在真实 MySQL 上实测**得出，不是推测。<br>**🚨 1（会毁数据，最高优先级）**：**严禁 `alembic downgrade base`**。真实库是 **MySQL `127.0.0.1/gpfx`，127 张业务表**；`downgrade base` 会沿迁移链一路回滚把它们**全部 DROP**。回滚验证只能用 `downgrade -1`。<br>**🚨 2**：本机真实库**不是**默认 SQLite——`config/db_config.json` 的 `use_mysql=true` 优先，`alembic/env.py:278-286` 会连 MySQL，`settings.database_url`（SQLite 临时文件）**根本不会被使用**。迁移验证必须设 `ALEMBIC_DATABASE_URL` 指向 scratch 库。<br>**🚨 3**：建表必须显式 `mysql_engine="InnoDB"`。本机 MySQL server **默认引擎是 MyISAM**，不指定就建成 MyISAM —— 而 `task_locks` 的原子获取依赖「唯一约束 + 事务」，**MyISAM 无事务 → 双锁失效**。实测不加此参数时 `init_db()` 会把 14 张表全部 `Converted ... to InnoDB`。<br>**🚨 4**：迁移里的**索引名必须与 ORM 一致**。ORM 的 `mapped_column(index=True)` 生成 `ix_<表名>_<列名>`；迁移若另起短名（`ix_fmr_status`）会**把同一索引建两遍**——实测多出 **20 个冗余索引**。正确做法：索引名从 ORM metadata 反推，不手写。<br>**🚨 5**：迁移的**列必须与 ORM 完全对齐**。首版漏了 `training_candidate_pool_snapshots` 的 3 个行业列，靠 `_auto_align_all_schema` 自动补上才没出事。<br>**⚠️ 6**：`sa.inspect()` 返回的是 **schema 快照**。建表后紧接着建索引时**必须重新 inspect**，复用 `upgrade()` 开头那个对象会因 `has_table=False` 而**静默跳过全部索引**（实测 14 张表索引全丢，且不报错）。<br>**⚠️ 7**：`_auto_align_all_schema`（`app/db/init_db.py:1535`）会**静默补齐**上面 3/4/5 的缺陷 —— 所以**「应用能跑」不等于「迁移是对的」**。验收必须跑 `verify_schema_drift.py` 确认 `新增表0 / 新增列0 / 新增索引0`。 |

#### T02 · 切分归一化哨兵单测 — 预算 M

| 项 | 内容 |
|---|---|
| **目标** | 把 `.workbuddy`/staging 里的 `_selfcheck_p0.py` 转为正式单测，**这条测试是全程不得变红的泄漏哨兵** |
| **前置** | T01 |
| **写** | `tests/services/factors/mining/test_evaluation_adapter_split.py` |
| **读** | SD-v2.0 §7.2 |
| **不做** | 不改 `evaluation_adapter.py` 的常量（若测试失败说明 T01 搬错，回去修 T01） |
| **DoD** | `.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_evaluation_adapter_split.py -q` exit 0，**≥8 passed** |
| **必测断言** | ① `normalize_purge_points` daily/weekly/monthly → 5/1/1 ② 折算交易日 daily 5→5、weekly 1→5、monthly 1→21 ③ daily n=1215 → val 238 / test 233 ④ weekly n=243 → val 48 / test 43 **且不抛异常** ⑤ monthly n=60 → val 11 / test 6 ⑥ monthly n=120 → val 23 / test 18 ⑦ 仅归一化、仍用默认 `min_validation_days=50` → **必须抛 `insufficient_dates`**（反向断言，防有人把默认值改掉） ⑧ `assert_no_leakage` 对 train 段外日期抛 `AssertionError` |
| **坑** | ⚠️ 用真实 `factor_evaluator.build_time_split`，**不要 stub**（staging 里那个 stub 只是为了让自检能离线跑）。看到 ④ 抛异常就说明**两处只改了一处**。 |

#### T03 · `cs_` 截面算子族 + 注册**四处** — 预算 M

| 项 | 内容 |
|---|---|
| **目标** | 6 个 `cs_` 算子可用于公式内，与后处理 `rank` 语义可区分 |
| **前置** | T02 |
| **写** | `app/services/factors/factor_compiler.py`、`app/services/indicator_ast_sandbox.py`、`app/services/factors/dsl/cross_section.py` |
| **读** | SD-v2.0 §6.2；`factor_compiler.py:370`(FIELD_CATALOG)、`:434`(FUNCTION_CATALOG)、`:467`(_ALLOWED_NODES)；`indicator_ast_sandbox.py:26`(ALLOWED_FUNCS) |
| **不做** | 不做行业内中性化（二期）；不改 `apply_postprocess` 既有语义；不做执行计划缓存 |
| **DoD** | ① `pytest tests/.../test_dsl_cross_section.py -q` exit 0 ② 断言 `cs_rank(A)-cs_rank(B)` 编译成功 ③ 断言后处理 `rank` 配置仍在且行为不变（回归） |
| **坑** | ⚠️ **注册四处联动**（原写「三处」，T03 实测漏了第 4 处）：① `FUNCTION_CATALOG` ② `FUNCTION_DIRECTION` ③ **`formula_catalog._FUNCTION_META`** ④ `indicator_ast_sandbox.ALLOWED_FUNCS`。第 ③ 处是**硬索引**（`formula_catalog.py:534` 的 `meta = _FUNCTION_META[key]`），漏键直接 `KeyError` → `build_formula_catalog()` 抛异常 → 因子中心公式目录 API（`factors.py:390/529`）500。**实证**：加 `FUNCTION_CATALOG['cs_rank']` 后调 `build_formula_catalog()` → `KeyError: 'cs_rank'`。⚠️ `cs_zscore` 在 std=0 的退化截面返回 **NaN**（不是 0），这是已冻结的决策，不要"修正"成 0。⚠️ **`ExecutionPlan.content_hash()` 是 `factor_versions.execution_plan_hash` 的去重身份键**（`factor_registry.py:236/267`），**禁止**把方向标注写进 `to_canonical_json()` / `formula_ast` —— 那会让既有因子版本哈希全部失效、同公式重复建版本。方向只作运行时查询（`collect_directional_calls`）。⚠️ `FUNCTION_DIRECTION`（求值轴）≠ `ExecutionPlan.direction`（因子方向 higher_better/lower_better），两个概念不要复用字段名。 |

#### T06 · 字段注册 +3（`prev_close` 保留）+ 25 模板编译率 — 预算 M（**门禁 G1**）✅ 已完成

| 项 | 内容 |
|---|---|
| **目标** | 注册 3 个字段（零采集）、25 个经典模板编译率 ≥90% |
| **前置** | T05 |
| **写** | `app/services/factors/factor_compiler.py`、`app/services/factors/formula_catalog.py`、`tests/.../test_template_compile_rate.py`、`tests/test_whitebox_formula_catalog.py` |
| **读** | SD-v2.0 §6.1/§12.3；需求文档 §2.3；**向导 §6.3.3（25 模板清单，逐字转录）** |
| **不做** | 不采集 `gross_margin`/`asset_turnover`（M2）；依赖它们的 2 个质量类模板**保持禁用**，不计入分母 |
| **DoD** | ✅ `pytest tests/.../test_template_compile_rate.py -q -s` **exit 0，26 passed**，编译率 **100%**（组合级 65/65、模板级 23/23） |
| **坑** | ✅ 原「`prev_close` 是虚注册、要撤销」**已撤销**（§3.6 D-D）—— 它是执行器投影出的**可用**虚字段，**不要删**。<br>⚠️ 分母口径：**禁用模板不计入分母**（23 = 25 − 2），否则因 2 个禁用单品单模板直接把上限压到 92%，再有一个编译失败就破线。<br>⚠️ `dividend_yield` 物理列存在但**全为 NULL**（需求 §2.3 写的「308 万行」是**表行数**）→ **不要写死 `data_mode=blocked`**，交给覆盖率检测自动判 blocked。<br>⚠️ 参数必须**穷举组合**（65 个变体），不是每模板试一组；strict_fields 模式也要跑。<br>⚠️ 本任务相关但有 2 个**既有红灯**在 `tests/test_whitebox_factor_executor.py`（非本任务引入，见 observations）。 |

#### T11 · 看板分析 + 冻结快照 + 锁定 — 预算 L（**门禁 G2**）

| 项 | 内容 |
|---|---|
| **目标** | 端到端产出一个冻结候选池快照 + 看板数据 + 锁定态 |
| **前置** | T09, T10 |
| **写** | `app/services/factors/candidate_pool/analysis.py`、`candidate_pool/service.py`（追加） |
| **读** | SD-v2.0 §6.3；向导详细设计 §3.7 |
| **不做** | 不做行业历史 PIT 筛选；不做非 A 股；看板同步计算（不引异步任务） |
| **DoD** | ① 端到端脚本：建池 → 加成员 → `analyze` → 断言返回 `snapshot_id` + `analysis_json` 含 6 个区块 ② 断言 `is_locked=1` ③ 断言 `reset-analysis` 后 `analysis_status=reset` 且 `is_locked=0` ④ 断言成员 <50 时 `analyze` 返回 4xx |
| **坑** | ⚠️ **`analysis_json` 是 AI 生成的 Prompt 输入**（向导 §3.7.5），字段名要与 T22 的 `build_prompt` 对齐，否则 AI 拿到空变量。⚠️ 看板行业分布只能读 `symbols.industry` **当前值**，快照要记 `industry_observed_at`，**严禁**用于中性化。 |

#### T22 · AI 骨架生成 — 预算 L

| 项 | 内容 |
|---|---|
| **目标** | 批量生成骨架，强制 4 字段输出，失败降级不阻塞 |
| **前置** | T18 |
| **写** | `app/services/factors/mining/ai_generator.py` |
| **读** | SD-v2.0 §7.5；决策记录 v3 §Q9；`app/services/ai/llm_client.py:206` |
| **不做** | 不做 F2 AI 引导变异（二期）；不改 `llm_client`（它是同步 def） |
| **DoD** | ① 单测：缺 `economic_logic` 或 `expected_direction` 的骨架被丢弃且名额回退随机 ② 单测：10 次调用上限生效（第 11 次不发起） ③ 单测：总超时 5 分钟触发降级 ④ 集成：AI 全失败时进化仍能跑完（不阻塞） |
| **坑** | ⚠️ `call_llm_with_failover` 是**同步 def**，必须 `asyncio.to_thread` 包，否则阻塞事件循环。⚠️ 并发 4 路用 `ThreadPoolExecutor`，不要用 `ProcessPool`。⚠️ 最坏情况（80 个叠满损耗剩 49 < 57）**属预期行为**，随机补足，不要当 bug 修。 |

#### T23 · GA 主循环（朴素）— 预算 L

| 项 | 内容 |
|---|---|
| **目标** | 20 代跑通，产出候选；M1 用朴素选择 + 固定三率 |
| **前置** | T19, T20, T21 |
| **写** | `app/services/factors/mining/genetic_algorithm.py`、`mining/service.py` |
| **读** | SD-v2.0 §6.7、§7.2、§7.8 |
| **不做** | 不实现在选择机制 A1/B1/B2（T33 才做）；不做并行 |
| **DoD** | ① 端到端：20 代跑完，`factor_mining_generations` 有 21 行 ② **断言每代 `probe_g2_hit_rate` 有值** ③ **断言 val/test 段日期未出现在任何 `evaluate_short` 的输入中** ④ 固定种子两次跑结果一致 ⑤ 精英个体 `operation='elite'` 且未被变异 |
| **坑** | ⚠️ 收敛判断用**连续 N 代提升 < 阈值**，**不是**固定 ICIR 门禁值。⚠️ `total_trials` 每代都要累加，M1 就要写（DSR 的唯一输入源）。 |

#### T24 · 最终验证 + 结果落库 — 预算 L

| 项 | 内容 |
|---|---|
| **目标** | Top N 全量评估，test 一次性，评估后禁止回炉 |
| **前置** | T23 |
| **写** | `app/services/factors/mining/service.py`（追加）、`evaluation_adapter.py`（`evaluate_full` 实现） |
| **读** | SD-v2.0 §7.2 |
| **不做** | 不重复 Step3 的深度字段扫描 |
| **DoD** | ① 断言 `factor_evaluation_runs` 写入 `test_start_date`/`test_end_date` ② 断言 test 段仅被评估一次（二次调用抛 `MINING_TEST_LOCKED`） ③ 断言 `run.status` 变为 `succeeded` 且不可回退到 `running` |
| **坑** | ⚠️ `evaluate_full` 需要 `factor_version_id`，而候选此时还没转正式因子 → 需先在 `factors` 建临时 draft（走 `create_factor_draft`，红线 C6），**不要**自己插 `factor_versions`。 |

#### T35 · 统计层 8 方法 — 预算 L

| 项 | 内容 |
|---|---|
| **目标** | t 检验/Bonferroni/FDR/Bootstrap/置换/DSR/衰减率/Walk-Forward |
| **前置** | T24 |
| **写** | `app/services/factors/mining/statistical_tests.py`、`requirements.txt`（加 scipy） |
| **读** | SD-v2.0 §7.9 |
| **不做** | **不引 `statsmodels`**（Bonferroni/FDR 自研）；不新建统计表（写 `metrics_json.stats`）；不做跨窗口合并检验 |
| **DoD** | ① 8 个方法各自与手工小样本对拍，误差 <1e-9 ② 断言 `scipy` 已在 `requirements.txt` ③ 月频输入 → `degraded=true` 且 `perm_p_value is None`、`ci_lower is None` ④ 断言 `metrics_json.stats` 结构含全部键 |
| **坑** | ⚠️ Bonferroni/FDR **不做双路径开关**，避免两条实现结果不一致。⚠️ Walk-Forward 窗口**重叠约 1.5 年，非独立样本**，只做方向一致性计数（≥3/4 同向），**不做合并检验**——做了就是高估显著性。⚠️ `scipy` 当前只是 sklearn 传递依赖，必须显式声明。 |

#### T36 · 质量分级 + 季度重评 — 预算 L

| 项 | 内容 |
|---|---|
| **目标** | 8 维度 S/A/B/C/D 五级 + 季度重评 |
| **前置** | T35 |
| **写** | `app/services/factors/mining/factor_grading.py`、`app/models/factor_grade_history.py`、`app/services/scheduled_tasks.py`（2 处追加）、`alembic/versions/0056_*.py` |
| **读** | SD-v2.0 §7.10；需求 §6.8 五级阈值表 |
| **不做** | 不引 statsmodels；不新建调度器（复用 `scheduled_tasks`） |
| **DoD** | ① `grade()` 纯函数五级边界单测（每级"全部满足"才定级 → 至少 5 组边界 + 5 组差一项） ② 断言 `decay_ratio<0.5` → 结果不得为 S/A/B ③ 断言月频 → 最高 B ④ 断言 `grade_manual_adjusted=1` 的记录不被季度任务覆盖 ⑤ 断言 `factor_grade_history` 每次评定落库 |
| **坑** | ✅ **原待决 Q3 已裁决（D-C / SD-v2.0 §12.3）**：月频**永久最高 B 级**，写进产品文案与设置页说明。日/周频 `test≥24` 约束**不动**。**代码禁止把 24 写死**，门槛做成可配置项（`DEFAULT_THRESHOLDS`）。⚠️ 分级判定是「每级全部维度满足才定级」，不是加权评分——任一项不满足即降级。 |

---

## 6. 门禁（可执行检查，不是评审会）

每个门禁是一条**必须 exit 0** 的命令。门禁未过不得进入下一阶段。

| 门禁 | 命令 | 期望 | 对应任务 |
|------|------|------|---------|
| **G0** | `.venv/Scripts/python.exe -m pytest tests/services/factors/mining/ -q` | exit 0；切分哨兵全绿；`alembic heads` = `wps_0023_051_factor_mining_core` | T01,T02 |
| **G1** | `... -m pytest tests/.../test_template_compile_rate.py -q -s` | exit 0；输出"编译率 ≥90%" | T06 |
| **G2** | `... -m pytest tests/.../test_candidate_pool_e2e.py -q` | exit 0；产出冻结快照 + `analysis_json` 6 区块 | T11 |
| **G3** | `... -m pytest tests/.../test_task_lock_concurrency.py -q` | exit 0；N 线程并发仅 1 成功；释放后队首被拉起 | T12,T14,T15 |
| **G4** | `... -m pytest tests/.../test_mining_e2e.py -q` | exit 0；20 代跑通；候选入库；test 仅评一次 | T25 |
| **G5** | 前端 `npm test -- FactorMining` | exit 0；5 步向导可点通 | T32 |
| **G6** | `... -m pytest tests/.../test_grading.py tests/.../test_statistical_tests.py -q` | exit 0 | T37 |
| **G7** | 前端 `npm test` 全量 | exit 0；`translations.test.ts` 绿 | T40 |

**门禁失败时的处理**：不要"降低门禁标准让它过"。把失败信息写进 `PROGRESS.json` 标 `blocked`，等人判。

---

## 7. 上下文预算（每个任务该读几个文件）

Agent 的失败常常不是"不会写"，而是**上下文塞太多导致后半段质量塌方**。硬约束：

| 任务预算 | 允许读的文件数 | 允许的 SD-v2.0 章节数 |
|---------|--------------|---------------------|
| `S` | ≤ 3 | ≤ 1 章 |
| `M` | ≤ 6 | ≤ 2 章 |
| `L` | ≤ 10 | ≤ 3 章 |

**超出就拆任务**，不要硬扛。

**允许"多次小会话"**：一个 `L` 任务可以拆成 2~3 次会话做，每次会话结束都更新 `PROGRESS.json` 的 `in_progress` + `notes`（写明"已完成 X，剩余 Y，下次从 Z 开始"）。**这比一次塞满更好。**

**禁止**：读整个 `docs/` 目录、读 `app/services/factors/__facade__.py`（3581 行）、读 `frontend/src/api/client.ts`（4585 行）全文。需要时用 Grep 定位符号再局部 Read。

---

## 8. 常见跑偏与纠正（多 Agent 实测高发）

| # | 跑偏现象 | 根因 | 纠正 |
|---|---------|------|------|
| 1 | **重新实现已有能力** | 没读 SD-v2.0 §2 的复用清单 | 先看 §2.1 核验表；`TimeSplit`/IC/ICIR/覆盖率/单调性/换手率/生命周期/FactorSet **全部现成** |
| 2 | **直接调 `build_time_split(...)` 传 purge=5** | 没读 §3.1 | 必须走 `normalize_purge_points()` + 显式传三个 `min_*` |
| 3 | **改了 L1 文件签名让下游编译不过** | 越权 | R2：只加可选参数，默认值保持原行为。改签名会破坏因子中心既有链路 |
| 4 | **test 段被反复评估** | 没意识到不可逆 | R6：test 一次性；`run.status → succeeded` 后回炉要抛 `MINING_TEST_LOCKED` |
| 5 | **把稀疏字段填零"让测试过"** | 图省事 | R10：禁止。宁可阻断（`MINING_SAMPLE_INSUFFICIENT`） |
| 6 | **两个 Agent 同时改 `factor_compiler.py`** | 没查写权限表 | §2.3：查 `PROGRESS.json` 的 `in_progress`，冲突就 `blocked` 等待 |
| 7 | **alembic 并行新建修订导致链断裂** | 都用了同一个 `down_revision` | 迁移必须串行；新建前跑 `alembic heads` 确认 head |
| 8 | **i18n 只加了 `zh-CN.ts`** | 忘了双份 | `translations.test.ts` 断言两侧 key 集合相等，会红 |
| 9 | **新增了第三方依赖"反正能用"** | 越权 | R3：`requirements.txt` 之外禁止。scipy 要显式加；`psutil` M3 才加；`statsmodels` 永不 |
| 10 | **"看起来对了"就标 done** | 没跑 DoD | §2.4：没跑命令不算完成。`evidence` 必须有 `cmd` + `exit` |
| 11 | **用 `hash()` 做缓存键** | 惯性 | R4：必须 SHA-256（内置 hash 有随机化种子，跨进程不一致） |
| 12 | **AI 生成失败当 bug 修** | 误判 | 最坏情况剩 49 < 57 随机补足，**属预期行为** |
| 13 | **月频 S/A 门槛写死 24** | 没读已裁决项 | **D-C 已裁决**（§3.6）：月频永久最高 B 级，门槛做成可配置。写死 24 即违约 |
| 21 | **把已裁决项当"建议"重新讨论** | 没读 §3.6 | D-A~D-D 四项已拍板。有异议**必须提新证据走一次裁决**，不得直接改代码 |
| 22 | **照抄 T03 的沙箱「拒绝」模式去处理 `ts_*`** | 没注意两者语义不同 | D-A 只针对截面算子（需面板上下文）。`ts_delta_bars` / `ts_atr` 在**逐标的**序列上有真实语义，T04 必须给 `ALLOWED_FUNCS` 提供**可用实现**（见 T04 卡） |
| 23 | **往 `PROGRESS.json` / `tasks.json` 追加同名任务键** | 没先搜是否已有占位 | 🚨 **会让该任务的记录无声消失**（重复键取后者 + 脚本写回时静默去重）。改 JSON 前先搜键名；写回前先跑 C19 重复键检测（R16） |
| 24 | **把 dsl 层的面板函数直接接到 `factor_executor`** | 没注意数据形状不同 | `_eval_ast` 的 context 是**按 index 对齐的 Series**，而 `dsl/cross_section.py` / `dsl/time_series.py` 要求 **MultiIndex(trade_date, symbol)** 的 DataFrame。中间必须有适配层；不要改 dsl 层去迁就 Series（会破坏 T03/T04 已冻结的契约） |
| 25 | **以为 executor 只需加一个截面分支** | 只读了任务卡标题 | 必须加**两个**：`cross_section` 与 `timeseries`。分派点在 `factor_executor.py:350-354`，漏任一个都落到 `return None` → 因子值**全 NaN 且不报错** |
| 26 | **`pd.DataFrame(series, index=multiindex)` 装填面板** | 不知道 pandas 会按**标签对齐** | 🚨 传入的 Series 是 `source_data` 的 RangeIndex，与面板 MultiIndex 标签不匹配 → **整列变 NaN** → 所有 `cs_`/`ts_` 算子静默全 NaN（而「结果不是 None」的断言照样通过）。必须用 `pd.DataFrame({'value': series.to_numpy()}, index=panel_index)` —— **按位置**装填。实测踩过（T05） |
| 27 | **只断言「结果不是 None」** | 图省事 | 「不是 None」挡不住**静默 NaN**。切分/截面/时序的断言一律要**数值精确**；另可用 `warnings.simplefilter("error", RuntimeWarning)` 探测 numpy 的 `Mean of empty slice`（全 NaN 的典型信号） |
| 28 | **顺手统一 `min_periods`** | 觉得不一致就是 bug | `_eval_rolling_func` 用 `min_periods=1`（允许部分窗口），`ts_atr` 刻意用 `min_periods=n`（前 n 期 NaN，**不造值**，需求 §3.5）。这是**有意**的差异，T05 已加断言锁死 |
| 14 | **改了 `evaluation_adapter.py` 的常量让测试过** | 本末倒置 | 常量是冻结契约。测试失败说明搬错/上游错，回去修上游 |
| 15 | **在真实库（MySQL gpfx）上跑 `alembic downgrade base`** | 以为是"测一下能不能回滚" | 🚨 **这会 DROP 掉 127 张业务表**。迁移验证一律用 `ALEMBIC_DATABASE_URL` 指向 scratch 库；回滚只测 `downgrade -1`（R11） |
| 16 | **以为库是默认 SQLite 就往里建表** | 没读 `db_config.json` | 真实库是 MySQL gpfx，`settings.database_url` 是**误导性的**（不会被使用，见 R12）。动 DB 前先确认连的是哪个库 |
| 29 | **用带 `head_limit` 的搜索证明「全仓不存在」** | 把「前 N 条没有」当成「全都没有」 | 🚨 这是 D-D 错误裁决的成因（R17）。判断不存在要用无上限搜索或正向验证 |
| 30 | **回归集合只跑本轮新增测试** | 图快 | 🚨 T04/T05 都改了 `factor_executor.py` 却没跑 `tests/test_whitebox_factor_executor.py`，2 个既有红灯连续两轮未发现（R18）。**改了哪个文件就跑覆盖它的测试** |
| 31 | **看到红测试就顺手修** | 想「让仓库干净」 | 若成因是**产品口径**（例：纯估值公式是否必须依赖日线 spine），改测试等于用测试掩盖设计问题。**按 R 规则记 `observations` 上报，不自行修**（本次那 2 个红灯即如此） |
| 32 | **给「列存在但全为 NULL」的字段写死 `data_mode=blocked`** | 觉得「没数据就 blocked」 | 写死会**永久阻断**，补数后也不会恢复。应交给覆盖率检测（`nonnull_rows<=0`）自动判 blocked，补数后自动转 limited |
| 36 | **以为迁移里写了 `ForeignKeyConstraint` 就一定建了外键** | 没意识到方言会静默忽略 | 🚨 **MyISAM 不支持外键且静默忽略定义**；事后转 InnoDB 也不会补建。实测真实库 14 张表 FK=0（同库其它表有 95 个 FK）。修法：修订 0054 补建 + 漂移检查加外键比对 |
| 37 | **只让漂移工具比对列与索引** | 觉得「主要结构都覆盖了」 | 🚨 **工具漏掉哪类结构，那类缺陷就会隐形**。T01 漏检 6 个外键就是因此。新增检查项时主动问「还有哪些 schema 对象没被比对」（外键 / 检查约束 / 存储引擎 / 字符集 / 默认值 / 列顺序） |
| 38 | **新建 `app/api/routes/*.py` 却没在 `app/api/router.py` 加 `include_router`** | 任务卡的写权限没写 router.py | 接口**404 且不报错**。T08/T25 的原卡片都有这个缺口。自检 C20 已兜住 |
| 40 | **把静态路径声明在 `{param}` 动态路径之后** | 没意识到 FastAPI 按**声明顺序**匹配 | `/candidate-pools/{pool_id}` 会吞掉后声明的 `/candidate-pools/filter-presets`（被当成 `pool_id="filter-presets"`）→ 404 或误报。T09 的 `filter-presets` / `preview` 必须放在 `{pool_id}` 之前 |
| 41 | **把挖掘域审计写进 `data_governance_audit_events`** | 觉得「有现成的审计表就用」 | 🚨 该表 `action` 上有 **DB 级 CHECK 约束**，新增动作要**改代码白名单 + 加一次迁移**，且语义属 portfolio/数据治理域。挖掘域写 **`factor_audit_logs`**（`FactorAuditLog`，`action` 自由字符串，AC-18 全写操作审计） |
| 42 | **以为 `FactorSevenError` 会自动变成 4xx** | 看名字像有全局处理器 | `app/main.py` **没有** `FactorSevenError` 的 handler → 未捕获会落成 **500**。路由必须 `except FactorSevenError` 并转 `HTTPException(status_code, detail=exc.to_dict())`；映射：`NOT_FOUND`→404、`BUSINESS_BLOCKED`→409、`VALIDATION_ERROR`→400 |
| 43 | **用 `db_session` 跑几十个用例时前台等** | 不知道 fixture 有多贵 | `db_session` 每例重建 `DatabaseManager` + `_auto_align_all_schema`，实测 **≈3.6 秒/例**；43 例 = **154 秒**，前台会撞工具超时被 SIGTERM 杀掉，**日志里看不到任何失败信息**（像「挂住」）。大文件测试一律 `run_in_background=true` |
| 44 | **幂等操作也写审计 / bump version** | 想「有变更就记一笔」 | 重复加成员、重复删成员、传同值 `source_type` 都属**无变更**，写审计会让审计表被噪声淹没、version 也失去「结构变更」语义。判据：`changed == 0` 时不写审计、不 bump version |
| 45 | **在服务返回前只断言「没抛异常」** | 图省事 | 软删除这类操作必须断言**副作用边界**：`symbols` 行数**不变**、成员行**仍在**（`is_deleted=1` 而非物理删除）。T08 的 `test_remove_members_is_soft_delete_only` 就是这个哨兵 |
| 39 | **验证外键只看 `information_schema`** | 觉得「查到了就是生效了」 | 「定义存在」≠「被强制」（MyISAM 的定义在、强制无）。必须做**行为验证**：孤儿行被 1452 拒、硬删父行级联生效 |
| 34 | **把含反引号的中文长文本经 `bash -c` 写文件（第二次犯）** | 图省事，觉得"这次短" | 🚨 T06 追加工作日志时**整段标识符被吞**：落盘成 `## T06 · 字段注册 +3（ 保留）+ …`、`- （26 passed / exit 0）`，而 shell 只报一堆 `xxx: command not found`、写入动作**返回 0**。**规则**：含反引号的内容一律先 `Write` 成 `.py` 再执行；写完**回读校验**（本次就是靠回读发现的）。兜底工具：`audit_text_integrity.py`（注意它有已知盲区，见其 docstring） |
| 35 | **给门禁加"看起来合理但误报率高"的规则** | 觉得抓得越多越好 | 🚨 本次做文本审计工具时，"全角括号后标识符消失"这条规则在 5 个真实文件上产生 **23 处误报、0 处真阳性** —— 与正常中文写作（`（**26 passed**）`、`（2026-09-16）`）**不可区分**。已删除并记录理由。**规则**：**噪声门禁比没有门禁更差**（会训练人忽略它）。加规则前先算"真阳性/误报"比，误报多的规则直接不要 |
| 33 | **在 Git Bash 里用 `cp` / `head` / `tail`** | 以为 POSIX 命令都在 | 本机 Git Bash **没有这三个命令**（`command not found`）。复制文件用 Python `shutil.copy2`；看输出尾部用 Python 读文件切片。另：`git stash push -- <path>` 在本仓库报 `not a valid object`，取基线版本用 `git show HEAD:<path> > 文件` |
| 17 | **`except MiningSplitError` 就以为覆盖了全部切分失败** | 没注意死区抛的是**裸 `ValueError`** | 在 `[declared_floor, effective_min)` 死区内 `build_split` 抛的是**未经包装**的 `ValueError`（见 T24/T25 pitfalls）。路由层必须**同时**接住裸 `ValueError`，否则用户看到 500 而不是 `MINING_SAMPLE_INSUFFICIENT` + `fix_link` |
| 18 | **用 `test_end − test_start` 当 test 样本数** | 没注意 `tail_loss` 口径 | `TimeSplit` 的**日期跨度**含 `target_horizon` 个尾部点（forward return 位移 → NaN 标签）；`SplitBudget.test_points` 已扣。四组实测均差 5，会多算 |
| 19 | **在 `tests/conftest.py` 的两道门禁面前撞墙** | 没先读 conftest | 见 R14：`app.models.factor_*` 是**字面文本**禁用；`app/models/factor_mining.py` 等 4 个新模型文件**不在豁免名单** |
| 20 | **顺手给 `tests/services/**` 建了 `__init__.py`** | 惯性 | R15：`writes` 是精确白名单，多建文件即越权；不加 `__init__.py` 也能正常 collect |
| 29 | **按 `MAX(trade_date)` 取数据截止日** | 没看实测证据 | 2026-09-07 的日线只有 **1 个标的**。必须按完整截面判定（`rules._resolve_as_of_date`），并回报证据 |
| 30 | **用 `symbols` 表取板块/上市日/行业** | 以为主表最权威 | `symbols.board` 是脏数据（与 `universe_symbols` 冲突 2,333 行），`listed_at`/`industry` 全空。筛选 universe 一律用 `universe_symbols` |
| 31 | **把「数据不足」当成「不亏损」** | 布尔语义没想清 | 向导 §3.4：缺失年度**不等于盈利**，必须排除。`rules._should_exclude_by_loss` 返回的是「该排除」不是「是亏损」 |
| 32 | **静态路由写在动态路由后面** | 不知道 FastAPI 按声明顺序 | 404 且无告警。新增端点后跑 `_verify_t09_wiring.py` 的顺序断言 |
| 33 | **给「根因类问题」报「症状」** | 按错误码查表挑一个抛 | 用户引用了无数据字段时若报「命中太少」，用户会去放宽区间，真正的问题永远发现不了。`assert_pool_generatable` 按 `blocking_issues` 顺序抛第一个 |
| 34 | **以为缺 `python-multipart` 只是上传功能不可用** | 没意识到它是应用级硬依赖 | FastAPI 的 `File(...)` 在 import 期检查 → **整个后端起不来**。要么装依赖，要么改 `Request` 手工解析（R22） |
| 35 | **自动补齐导入代码的前导零** | 图用户体验 | 补零会静默改变用户意图（`1` 也可能是无效输入）。改为在 `format_error` 里给可操作提示（同 R10 精神） |
| 36 | **用 `a or b or c` 串联多个候选列名** | 不知道命中后返回的是 pandas Series | `Series or ...` 抛 `ValueError: truth value of a Series is ambiguous`。要用 `for k in keys: if k in col_map` |
| 37 | **把「与主数据不一致」和「不是已知值」写成 elif** | 想省一个分支 | 两个条件可同时成立，`elif` 会吞掉后者。校验分支要独立 |
| 38 | **用 `canonical_json` 落库「要原样还原」的数据** | 只知道它是「规范化序列化」 | 它会**静默丢弃所有 None**（`{'a': None}` → `{}`）。算哈希没问题；冻结/快照/审计落库会丢数据。用 `service._freeze_json`（R23） |
| 39 | **用真实 MySQL 做冒烟测试** | 觉得「反正是读」 | T11 冒烟往 `symbols` 写了 61 行测试数据，事后专门清理。候选池域「不回写主数据」对**测试**同样成立，一律用 `db_session`（SQLite） |
| 40 | **在冻结时就锁定** | 把 §3.7.1 的流程压成一步 | 锁定发生在**分析完成**（`mark_analyzed`），冻结只写成员。测试 `test_freeze_captures_members_and_does_not_lock` 钉死 |
| 41 | **让 `analyze_pool` 每次都新建快照** | 没考虑按钮重复点击 | 幂等复用「未分析且未锁定」的快照，否则堆空快照 |
| 42 | **池内 z-score 当风格暴露** | 忘了均值恒为 0 | 风格暴露必须用**全市场分位数**做基准（`analysis._style_scores`） |
| 43 | **心跳超时回收直接 DELETE 锁行** | 没意识到队列存在锁行里 | queue_json 随行删 → 排队任务**永久卡死**。必须走「删行+拉起队首」（`expire_stale_locks` 已修，回归哨兵在 test_task_lock_concurrency.py） |
| 44 | **以为 `FOR UPDATE` 在所有数据库都生效** | 只在生产库验证过 | SQLite 静默忽略 → 测试环境并发行为与生产不同。临界段补进程内锁（R24） |
| 45 | **给「要丢数据」的参数组合留静默路径** | 觉得没人调用就不管 | `release_lock(trigger_queue=False)` 会丢整个队列 —— 已改为抛 ValueError。显式失败优于静默丢任务 |
| 46 | **构造「两个挖掘任务排队」来测唤醒链** | 忘了 mining_domain 全局单任务 | 第二个提交会被 409 拒绝。duckdb_write 排队服务于不占挖掘域的任务类型（§6.5）；唤醒链用「手工建 queued 任务 + acquire_write_slot」构造 |
| 47 | **轮询 worker 结果时用 fixture session 直读** | 忘了跨线程 | worker 在另一个 session 提交，fixture 持有旧快照 → 永远轮询到 running。每轮 expire_all（R25） |
| 48 | **fixture teardown 不清全局 stop 事件** | 以为事件是测试局部的 | `WORKER_STOP_EVENT` 是模块级全局，不清会毒化本文件后续所有测试（全部 cancelled） |
| 49 | **worker 只查全局 stop 事件不查 cancel_requested** | 以为 cancel 会 set 全局事件 | `cancel_async_task` 对 running 只置 `cancel_requested=1`，靠 worker 自止。新建 worker 必须双信号都响应（R26，T14 真实踩中） |
| 50 | **校验类任务也去抢 duckdb_write** | 没细读 §7.1 | 该锁是「所有**写** DuckDB 的任务」；字段校验只读，不需要锁，更不占 mining_domain |
| 51 | **worker 跑完就断言锁已清空** | 忘了 finally 在置 done 之后 | 竞态窗口真实命中过。断言锁前轮询等锁清空（`_wait_locks_clear`） |
| 52 | **resume 新建任务不带旧进度** | 以为「同 payload」就等于续跑 | batch_recovery_json 在旧任务行上，新任务为空 → 从零重跑。建新任务后先复制进度再启 worker（R27，T14/T15 连续踩中） |
| 53 | **多线程测试共用 fixture session** | 以为 Session 线程安全 | 并发使用抛 `InvalidRequestError: provisioning a new connection`。每线程独立 session（sessionmaker(bind=bind)，用完即关） |
| 54 | **把 mirror 的调用记录当「已完成块」** | 失败块也被调用过 | 「已完成」的唯一依据是 recovery 的 completed_chunks；调用记录只能证明「尝试过」 |
| 55 | **配置哈希把列表里的 None 也删掉** | 以为「None 一律等价于缺失」 | 列表位置有语义：`['close', None]` ≠ `['close']`。字典去 None、**列表保留**（T16 parity 测试抓出） |
| 56 | **同一算法两处各写一份** | 觉得「先本地实现，以后再抽」 | 任一侧调整就静默分叉（幂等失效、不报错）。要么收敛到一处，要么加 parity 测试（R28） |
| 57 | **检验「旧算子残留」用子串匹配** | `"rank(" not in formula` 看着直观 | 归一后的 `cs_rank(` 会被命中 → 断言必红。用边界正则 `(?<![a-z_])rank\(` |
| 58 | **归类只用字段或只用算子** | 以为字段够用 | trend/reversal/volatility 共用 close/high/low，字段判不开；quality/valuation 靠字段、volatility 靠算子 —— 必须联合判定（T17） |
| 59 | **按名字前缀/直觉判断算子量纲** | `cs_` 前缀看着都像「截面归一化」 | 判据是「**是否除以了同量纲的量**」：`cs_scale`（x/Σ|x|）**是无量纲**、`cs_demean`（x-μ）保持量纲 —— 我第一版把 cs_scale 归错（T18 复核修正）。归一化=rank/zscore/scale/quantile/stddev/pct_change/count；保形=demean/winsorize/mean/sma/ema/sum/highest/lowest/ref/ts_delta_*/ts_atr/abs/round |
| 60 | **多样性排序同分时按复杂度升序** | 觉得「简单优先」 | 相似度全 0 时 `abs(roe_ttm)` 这类平凡式占满前排，违背「随机生成探索非显然组合」的本意。同分应**复杂度降序** |
| 61 | **用「算子+字段集合」判结构是否相同** | 觉得集合一致就是同一结构 | 缺**保序骨架**维度 → `mean(a)/mean(b)-1` 与 `(mean(a)-mean(b))/b` 相似度 1.0，误杀一个因子（T19 实测）。形状键要保留字段名、只抽象数字（R31） |
| 62 | **新增「替代旧实现」的函数后没改调用点** | 只写了函数与单测 | 新函数形同虚设，旧路径继续错（T19 踩到：`structural_similarity` 没接进聚类循环）。R29 的复发形态 —— **必须让原失败用例转绿才算接线完成** |
| 63 | **把数字常量当子表达式提取** | 遍历 AST 时不区分节点类型 | 要求 compute_fn 去「算」`20`，且 `probe_subexpr_total` 虚高。默认跳过 Constant（编译期常量组装时内联） |
| 64 | **在渲染器里顺手去掉冗余括号** | 觉得「一步到位」 | 渲染就不再无损，基于渲染的断言失效。渲染管正确、规范化管唯一（R32） |
| 65 | **自检/守卫里写死期望值（魔数）** | 断言「渲染结果包含 X」，X 抄自当前输出 | 数据一变就误报（本次：`"已裁决项 4 项"` 硬编码，加到 8 条即失败）。**期望值必须从输入推导**（`f"已裁决项 {len(items)} 项"`） |
| 66 | **追加型收工脚本不幂等** | 觉得「就跑一次」 | 沙箱升级重试/中断重启会执行两次 → 手册与日志各被追加两份（本次真实发生）。写入前先判重（`if marker not in text`） |
| 67 | **把 `inf` / `1e308` 写进 MySQL `FLOAT` 列** | 用 inf 表达「不可比较」最直观；第一次修正只换成 `1e308` | inf 被 PyMySQL 客户端拒；`1e308` 超单精度上限被 `1264` 拒（我第一版修正**同样无效**）。哨兵取 `3e38`（R33、R34） |
| 68 | **用通用归一化函数处理需要特殊值的列** | 图省事复用 `_safe_float` | 它把 NaN/Inf 归 0 → 哨兵语义被抹平。特殊列显式绕开 |
| 69 | **以为 SQLite 单测能证明 MySQL 数值范围安全** | 单测全绿就放心 | SQLite `REAL`=DOUBLE 且无范围检查，`inf`/`1e308` 照样写入；MySQL `Float` 是单精度 → 真实库才暴露（R34） |
| 70 | **把 `NaN` 直接赋给 MySQL 数值列** | 以为「NaN 只是个数」 | PyMySQL 客户端抛 `ProgrammingError: nan can not be used with MySQL`（FLOAT/DOUBLE 都不行）→ 整条写入失败。NaN 在指标计算里极常见，写库前必须归一化（转 None 或哨兵） |
| 71 | **夹取上界照抄真实上限（四舍五入值）** | 觉得 `3.4028235e38` ≈ 上限 | 它**大于**真实上限 `3.402823466e38` → 夹到边界反而 `1264` 被拒（P0 首版实测 11/15 失败）。留 ~0.08% 余量取 `3.4e38`（R35） |
| 72 | **把「归一化」做成会抛异常的函数** | 觉得「无法确定上界就该报错」 | 调用方被迫处处 try，风险被推给每个写入点。归一化工具应是「永不抛（除参数非法），输出永远有限或 None」，无列型信息时按**保守单精度**处理 |
| 73 | **直接 `await` 同步 I/O 函数** | 忘了它是 `def` 不是 `async def` | 事件循环被阻塞，「并发 N 路」静默退化成串行（不报错、只是慢）。用线程池 + `run_in_executor`，并用线程名/并发水位断言验证（R36） |
| 74 | **给平坦的适应度曲线期望「跑满代数」** | 忘了收敛看「提升量」 | 提升=0 → 连续 N 代无提升即收敛（任务卡坑 1 的正确行为）。测试断言必须与收敛语义一致：要跑满代数，就得让适应度持续提升 |
| 75 | **按「实际某类操作条数」记配额账** | 随机供给缺位时其它算子兜底 | 配额之和 ≠ 非精英名额。统计口径在写入侧定义一次：记**分配名额**（R37） |
| 76 | **测试造数据用同前缀哈希** | 觉得哈希值无所谓 | 草稿 code 取 `digest[:12]` → 同前缀的候选全部 code 冲突被幂等复用，表现为「全部同版本/全部失败」的假象。造数也要像真数据（sha256 互异） |
| 77 | **`json.dumps` 直接序列化含 NaN 的指标** | Python 允许就以为合法 | 产出非标准 JSON（`NaN` 字面量）→ 前端 `JSON.parse` 炸。落库前过 `db_numeric`，JSON 承载的指标建议单独立项清洗 |
| 78 | **多段最小值配置与消费方派生公式不自洽** | 各段独立设数，没核派生值 | `build_time_split` 会推 `derived_min_total = ceil((min_val+purge+embargo)/val_ratio)`，若 > 声明地板则死区内「预算达标但切分炸」。修复 = 分段先定、总门槛反推，保证 derived == 声明（D-I，SPLIT_MINIMUMS 新三元组） |
| 79 | **用户输入未清洗就当 code 用** | 觉得哈希是「干净」的 | 哈希可能含连字符/大写，`FactorDraftCreate.code` 的 Pydantic pattern `^[a-z][a-z0-9_]*$` 会在运行期才炸。入口处清洗（`[^a-z0-9_]` → `_`） |
| 80 | **`return X from Y`** | 想保留异常链又想返回异常对象 | `from` 只跟在 `raise` 后。返回异常对象时去掉 `from`，由调用方 raise 时建链 |

---

## 附录 A · 机器可读文件与选择器

本手册配套三个文件，放在 `.workbuddy/mining/`：

| 文件 | 作用 | 谁写 |
|------|------|------|
| `tasks.json` | 40 个任务的机器可读索引（id / deps / 产物 / DoD / 预算 / 门禁 / 坑 / **reads**） | **人**（本手册生成时产出），Agent 只读 |
| `PROGRESS.json` | 实时状态账本 | **Agent 读写**（每次开工/收工） |
| `next_task.py` | 任务选择器：读上面两个，输出"现在可开工的任务" | Agent 只读执行 |
| `update_progress_doc.py` | 把账本渲染成 `docs/因子挖掘-开发进度看板.md` | Agent 收工时执行（收工仪式第 4 步） |
| `verify_schema_drift.py` | **迁移 ↔ ORM 漂移检查**：比对库实际结构与 ORM 声明，暴露 `_auto_align` 掩盖的迁移缺陷 | **每个新增迁移的任务收工前必跑**（T01/T07/T26/T36） |
| `_selfcheck_conflict.py` | **排期自身的自检**：16 项（冲突检测、依赖图无环、DoD 完整性、粒度纪律、原始文档引用） | 人（改完 `tasks.json` 后跑一次） |
| `_selfcheck_progress_doc.py` | **看板渲染自检**：7 个场景 / 62 项（含 evidence 的 str 与 dict 两种写法） | 人（改完生成器后跑一次） |
| `verify_schema_drift.py` | **迁移漂移检查**：直接比对库 vs ORM（不启动应用、不跑 auto_align）。覆盖**列 / 索引 / 唯一约束 / 外键** | 新建迁移的任务收工前必跑（T26/T36） |
| `audit_text_integrity.py` | **文本完整性审计**：查「反引号被 shell 吞掉」的残骸；`--selftest` 自检 | 用 `bash -c` 写过中文文档后跑一次 |
| `_selfcheck_p0.py` | 切分归一化 P0 修复自检（骨架时期的离线校验脚本） | 人（已并入 T02 正式单测） |
| `_migrate_reads_v1.py` | 一次性迁移（已执行完），记录原始文档映射规则 | 人（已归档，可删） |

**新增迁移的任务必跑这条**（T01/T07/T26/T36）：

```bash
# 真实库
.venv/Scripts/python.exe .workbuddy/mining/verify_schema_drift.py --from-revision <迁移文件名>
# scratch 库（验证全新安装）
ALEMBIC_DATABASE_URL="sqlite:///<scratch>.db" .venv/Scripts/python.exe \
  .workbuddy/mining/verify_schema_drift.py --url-from-env --from-revision <迁移文件名>
```

> **为什么必须跑**：`app/db/init_db.py:1535` 的 `_auto_align_all_schema()` 会在启动时**静默补齐**
> 缺失的表/列/索引。好处是应用不会崩，**坏处是它掩盖了迁移文件本身的错误**。
> T01 实测：首版迁移的 3 类缺陷（MyISAM 引擎、20 个冗余索引、3 个缺列）**全部被 auto-align 悄悄补上**，
> 应用照常启动，直到跑这个脚本才暴露。

**产出的人可读文档**：`docs/因子挖掘-开发进度看板.md`——**自动生成，勿手工编辑**。
包含：总览进度条 / 8 个门禁状态 / 8 个阶段进度 / 40 任务明细表（含**决策**列）/ 进行中 / 阻塞项 / 变更日志 / 观察记录。

**两条自检命令（改完排期或生成器后必跑）**：

```bash
"D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe" .workbuddy/mining/_selfcheck_conflict.py
"D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe" .workbuddy/mining/_selfcheck_progress_doc.py
```

`_selfcheck_conflict.py` 覆盖 16 项：路径冲突单元行为、跨任务冲突（含 `high_conflict_files` 兜底）、
6 个真实调度场景（T01‖T27 可并行、T07 在跑时 T15 被**写权限**而非依赖挡住、alembic 串行、i18n 串行）、
依赖 ID 存在性、依赖图无环、DoD 完整性、写权限完整性、粒度纪律、**reads 路径存在性**、
**40/40 任务引用原始文档**、`source_docs` 完整性、已知错误清单 ≥8 处。

**用法**：

```bash
# 我该做什么？
"D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe" .workbuddy/mining/next_task.py

# 看某个任务详情
... next_task.py --task T23

# 看整体进度
... next_task.py --status

# 看哪些任务被阻塞、卡在谁身上
... next_task.py --blocked

# 按写权限过滤：把我的产物与在跑任务冲突的候选排除
... next_task.py --safe
```

`next_task.py` 的选择逻辑：
1. 收集 `status == pending` 的任务
2. 排除**前置未全部 `done`** 的
3. 排除**产物与任何 `in_progress` 任务产物重叠**的（`--safe` 时）
4. 按**门禁优先 → 预算小优先 → ID 小优先**排序，输出前 3 个

**没有可开工任务时**，脚本会明确告诉你原因（是"前置未完成"、"写权限冲突"还是"全部完成"），不要让 Agent 自己猜。

## 附录 B · 文档全景与关系

项目内与因子挖掘相关的文档共 **8 份**，分三类。**Agent 只需读第 1、2 类中指定的部分**。

### B.1 需求与设计类（**Agent 的输入源**）

| 文档 | 面向 | 权威范围 | 状态 | Agent 读法 |
|------|------|---------|------|-----------|
| `因子挖掘系统-开发需求文档-落地版.md` | 人 + Agent | **需求规则**：阈值、约束、验收、字段可用性 | 现行（**含 3 处已知错误，见 §1.3**） | 按任务卡 `reads` 指定章节读 |
| `因子挖掘系统-开发文档-落地版.md` | 人 + Agent | **技术落点**：表结构、接口、算法步骤、复用清单 | 现行（**含 3 处已知错误**） | 同上 |
| `因子挖掘实验向导-详细设计.md` | 人 + Agent | **UI 细节**：交互、锁定态、文案、布局 | 现行（**含 2 处已知错误**） | 同上 |
| `因子挖掘系统-系统设计文档-v2.0.md` | 人 + Agent | **技术事实来源**（全部经代码实测 + 已勘误前三份） | **现行，优先级最高** | 任务卡 `reads` 必含其章节 |

> **这三份原始文档 + SD-v2.0 的分工与优先级规则见 §1.2**；8 处已知错误清单见 §1.3。
> `tasks.json` 里 40/40 个任务的 `reads` 都已补入对应的原始文档章节（共 91 条引用）。

### B.2 排期与接续类（**本手册及其配套**）

| 文件 | 面向 | 作用 |
|------|------|------|
| `因子挖掘-AI-Agent开发排期与接续手册-v1.0.md` | **Agent** | 本文件。接手须知 / 接续协议 / 冻结契约 / 任务卡 / 门禁 |
| `docs/因子挖掘-开发进度看板.md` | **人** | 进度看板，**由 `PROGRESS.json` 自动生成，勿手工编辑** |
| `.workbuddy/mining/tasks.json` | Agent（只读） | 40 任务机器可读索引，含 `source_docs` 优先级规则与 `reads` |
| `.workbuddy/mining/PROGRESS.json` | Agent（读写） | 状态账本，**唯一进度事实来源** |
| `.workbuddy/mining/next_task.py` | Agent（执行） | 任务选择器（含写权限自动排除） |
| `.workbuddy/mining/update_progress_doc.py` | Agent（执行） | 账本 → 进度看板渲染器 |
| `.workbuddy/mining/_selfcheck_conflict.py` | 人（改排期后跑） | 排期自检，16 项检查 |
| `.workbuddy/mining/_selfcheck_progress_doc.py` | 人（改生成器后跑） | 看板渲染自检，4 场景 |

### B.3 历史与被取代类（**Agent 不要读**）

| 文档 | 面向 | 状态 | 为什么不要读 |
|------|------|------|-------------|
| `因子挖掘系统-底层架构设计文档-v1.0.md` | 人 | **已被 SD-v2.0 取代** | 含 2 项 P0 错误（purge 单位歧义、默认 `min_validation_days`），照做会写出跑不起来的代码 |
| `因子挖掘系统-工作安排与WBS-v1.0.md` | 人类团队（12 人） | 资源规划 | 人力估算与排期口径**不适用于 Agent**，任务切分/写权限/验收方式全都不同 |
| `因子挖掘系统-开发计划文档-执行版-v1.0.md` | 人类团队（4+3 人） | 执行计划 | 门禁是**评审式**（G1 三条判据靠人判），本文件的门禁是**可执行命令** |
| `因子挖掘-决策记录-v3.md`、`因子挖掘需求审核报告.md` | 人 | 决策链存档 | 仅供人回溯"为什么这么定"，Agent 不需要，且 Q1~Q10 结论已固化进 SD-v2.0 |
| `因子挖掘V1.0探索/`（目录，3 份） | 人 | 历史草稿 | 已被 v3.x 取代 |

### B.4 为什么不能直接套用人类排期

| 维度 | 人类排期 | 本文件（Agent） |
|------|---------|---------------|
| 上下文传递 | 会议 / 口头 / 交接文档 | **`PROGRESS.json` 的 `decisions` 字段**，必须落盘 |
| 任务粒度 | 人日（1 人日 = 大任务） | **单会话（≤3h，≤8 文件，≤3 条 DoD）** |
| 验收方式 | 评审会议、代码 review | **可执行命令 + exit code**；没跑等于没做 |
| 并行约束 | 团队分工、协商 | **写权限文件级互斥**，脚本自动排除冲突 |
| 失败处理 | 找人问 | **标 `blocked` + 结构化理由 + 停手**，禁止自行发挥 |
| 上下文预算 | 无此概念 | **硬约束：S≤3 / M≤6 / L≤10 个文件** |
| 文档优先级 | 以最新文档为准 | **分层：技术→SD-v2.0，需求/UI→原始文档，冲突→SD-v2.0**（§1.2） |

---

*本手册基于 SD-v2.0 与仓库实测环境编制（2026-09-16）。环境陷阱（Bash 损坏、PowerShell stdout 吞、venv 位置、根目录垃圾文件）均为实测结论，非推测。*