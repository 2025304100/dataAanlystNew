# 因子挖掘 v2.0 · 代码骨架（staging）

> ## ⚠️ 本目录已于 2026-09-16 被 T01 执行完毕，现为**历史存档**
>
> 骨架文件已**实际搬入仓库**（`app/models/`、`app/services/factors/{mining,dsl}/`、
> `app/api/routes/factor_mining.py`、`alembic/versions/2026_09_16_0053_*.py`），
> 并且**已在真实库 MySQL `gpfx` 建表成功**、双库零漂移。
>
> **不要再用本目录的副本改代码** —— 它改完不会生效，且会与 `app/` 下的真身发散。
> 后续所有改动请直接改 `app/` 下的文件。本目录只用于追溯"当时设计了什么"。
>
> 唯一仍然有用的文件是 `_selfcheck_p0.py`（切分归一化 P0 的离线自检），
> 它的正式版本是 T02 要产出的 `tests/services/factors/mining/test_evaluation_adapter_split.py`；
> T02 完成后本文件也可一并删除。

---

本目录是《因子挖掘系统-系统设计文档-v2.0.md》附录 A 对应的**代码骨架**。

## 原始设计意图（保留供追溯）

为什么当初放在 staging 而不是直接写进 `app/`：

`app/models/__init__.py:84` 有 `_auto_discover_models()`（pkgutil 扫描整个 `app/models/`），
`app/db/init_db.py:1535` 有 `_auto_align_all_schema()`。

**一旦把 `app/models/factor_mining.py` 放进 `app/models/`，下次应用启动就会自动在用户的
数据库里创建 22 张新表**（并修改 `factor_evaluation_runs` / `factor_versions` 的列）。
这属于会改动用户数据结构的副作用，不应在评审通过前单方面发生。

→ 所以先落 staging，评审通过后由 T01 一次性搬入并跑迁移。
**该流程已于 2026-09-16 执行完毕，见上方存档声明。**

> 执行结果：真实库 127 → 141 表（+14，无丢失），`factor_evaluation_runs` 29 → 31 列，
> 行数 5/25/17 全部不变。详见 `.workbuddy/mining/PROGRESS.json` 的 T01 条目。

## 目录映射

| staging 路径 | 目标路径 | 里程碑 |
|---|---|---|
| `app/models/factor_mining.py` | `app/models/factor_mining.py` | M1a |
| `app/models/mining_candidate_pool.py` | `app/models/mining_candidate_pool.py` | M1a |
| `app/models/task_lock.py` | `app/models/task_lock.py` | M1a |
| `app/services/factors/mining/contracts.py` | 同路径 | M1a |
| `app/services/factors/mining/evaluation_adapter.py` | 同路径 | M1a ★ |
| `app/services/factors/mining/task_lock.py` | 同路径 | M1a |
| `app/services/factors/mining/task_runner.py` | 同路径 | M1b |
| `app/services/factors/mining/genetic_algorithm.py` | 同路径 | M1b |
| `app/services/factors/dsl/cross_section.py` | 同路径 | M1a |
| `app/api/routes/factor_mining.py` | 同路径 | M1a/M1b |
| `alembic/versions/2026_09_16_0053_wps_0023_051_factor_mining_core.py` | 同路径 | M1a |
| `frontend/src/types/mining.ts` | 同路径 | M1a |
| `frontend/src/api/factorMining.ts` | 同路径 | M1a |
| `frontend/src/components/factors/mining/MiningShell.tsx` | 同路径 | M1a |

## 搬入后必须同步的 5 处存量文件（详见设计文档 §9.1 / 附录 C）

1. `app/api/router.py:3` —— import 列表加 `factor_mining` 等 5 个模块
2. `app/api/router.py:75` 之前 —— `api_router.include_router(factor_mining.router, tags=["factor-mining"])` ×5
3. `app/schemas/errors.py` —— `ERROR_CODE_LIBRARY` 加 7 个挖掘错误码
4. `alembic/env.py:34` —— 加 `from app.models import factor_mining  # noqa: F401`
5. `requirements.txt` —— 加 `scipy>=1.13`、`openpyxl>=3.1`

前端另需改 `Settings.tsx` **6 处**（含 `:76` 的 `settings:navigate` 白名单，最易漏）。

## 骨架的完成度约定

| 记号 | 含义 |
|------|------|
| 已实现 | 确定性纯逻辑（切分归一化、锁的原子获取、截面算子、预算计算）——可直接用，已有单测断言点 |
| `raise NotImplementedError` | 需按设计文档对应章节实现的业务逻辑，签名已冻结 |
| `# TODO(Mxx)` | 指向设计文档模块卡编号 |

**签名已冻结**（设计文档 §8.7 / §4.5），实现时不得改名/改参数顺序。

## 立即可跑的自检

`_selfcheck_p0.py` 用 stub 替换 L1 依赖，直接验证 P0 修复。**已实测通过**：

```
D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe docs/factor-mining-v2-skeleton/_selfcheck_p0.py
```

实测输出（摘要）：

| 检查 | 结果 |
|------|------|
| B9 `normalize_purge_points(daily/weekly/monthly)` | 5 / **1** / **1** ✅ |
| B9b 折算交易日数 | daily 5点=5日、weekly 1点=5日、monthly 1点=**21**日 ✅ |
| B10 daily n=1215 | train 729 / val 238 / test 233 ✅ |
| B10 weekly n=243 | train 145 / val **48** / test **43** ✅（显式传参前必抛异常） |
| B10 monthly n=60 | train 36 / val **11** / test **6** ✅（degraded=True） |
| B10 monthly n=120（10年镜像） | train 72 / val **23** / test **18** ✅ |
| 复现 P0 | 仅归一化、仍用默认 `min_validation_days=50` → **周/月频全部抛 `insufficient_dates`**（`derived_min_total=260`） |
| 月频 S/A 可达性 | 10 年镜像 test=18 < 需求 §4.1 门槛 24 → **不可达**（待决 Q3） |

> 结论：**C11（purge 归一化）与 C12（显式传 `min_*`）缺一不可**，只改一处无法让周/月频跑通。

搬入 `evaluation_adapter.py` + `contracts.py` 后，本脚本可原样挪到
`tests/services/factors/mining/test_evaluation_adapter_split.py` 作为正式单测
（把 stub 段替换为真实 import 即可）。
