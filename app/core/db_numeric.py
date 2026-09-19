"""MySQL 数值列写入安全（NaN / Inf / 范围归一化）。

为什么必须有这个模块（2026-09-17 实测）
=====================================
MySQL + PyMySQL 对「特殊浮点值」的拒绝**发生在客户端**，且**报错很难与业务错误区分**：

| 写入值 | MySQL `float` | MySQL `double` | SQLite `REAL` |
|---|---|---|---|
| `NaN` | ❌ `ProgrammingError: nan can not be used with MySQL` | ❌ 同 | ✅ |
| `inf` / `-inf` | ❌ `ProgrammingError: inf can not be used with MySQL` | ❌ 同 | ✅ |
| `1e308` | ❌ `DataError 1264 Out of range` | ✅ | ✅ |
| `3e38` | ✅ | ✅ | ✅ |

两条关键事实：

1. **`NaN` 比 `inf` 危险得多** —— 指标计算里 NaN 是常态（IC 样本为 0、波动为 0、无数据），
   而 `inf` 罕见。**一次 `NaN` 直写就会让整条 INSERT/UPDATE 失败**。
2. **SQLite 单测天然放行**（`REAL` 是 8 字节 DOUBLE 且不做范围检查）→
   「单测全绿」**不能证明**写入安全（铁律 R34）。故本模块配**基于列型上界**的断言式测试。

另一个反复踩到的坑：SQLAlchemy 的 `mapped_column(Float)`（不带 `precision`）在 MySQL
建出来的是 **`float` 单精度（上限 ≈3.4028e38）**，不是 `double`；`1e308` 会被拒。
→ **不要按 ORM 声明猜列型**；本模块的 `column_max()` 会按列对象的实际类型推断。

两条默认策略（**这是裁决，不是随手选的**）
=====================================
- **`NaN` / `None` → `None`**：项目里 NaN 的语义是「无效/缺失」。**绝不能归 0** ——
  例如 IC=0 的含义是「无预测力」，与「没算出来」完全不同，归 0 会污染统计与后续判定。
- **`±Inf` → `None`（默认）/ 哨兵（可选）**：`inf` 通常意味着上游除零/溢出（属异常，
  应当作缺失处理）。但少数列**用哨兵表达「不可比较/无限」是有意义的**
  （如 `cache_validation_max_diff`）——那类列显式传 `on_inf="clamp"`。

用法
====
    # 1) 单个值（最常用）：NaN/Inf → None
    model.ic_mean = to_db_float(raw_ic)

    # 2) 已知列对象 → 自动按列型夹取范围
    model.best_icir = to_db_float(raw_icir, column=Model.best_icir)

    # 3) 哨兵语义（不可比较 → 极大有限值）
    model.cache_validation_max_diff = to_db_float(
        raw_diff, column=Model.cache_validation_max_diff, on_inf="clamp")

    # 4) 批量：把指标 dict 里的 NaN/Inf 全部转 None 再落库
    for k, v in clean_numeric_fields(metrics).items():
        setattr(row, k, v)
"""
from __future__ import annotations

import math
import sys
from typing import Any, Mapping

#: MySQL `FLOAT`（单精度）的**可写安全上限** —— SQLAlchemy `Float`（无 precision）在 MySQL 就是它。
#:
#: ⚠️ 刻意取 `3.4e38` 而**不是**四舍五入后的 `3.4028235e38`：后者**大于** MySQL 真实上限
#: （实测：`3.402823466e38` ✅ 可写、`3.4028235e38` ❌ `1264 Out of range`）——
#: 夹到「上限」反而越界，是 P0 首版真踩到的坑。留 ~0.08% 余量最稳。
MYSQL_FLOAT_MAX = 3.4e38

#: MySQL `DOUBLE`（双精度）的可写安全上限（实测 `1.7976931348623157e308` 亦可写，
#: 但取整数量级值更稳、更易读，且 1e308~1.8e308 之间的真实数据几乎不存在）
MYSQL_DOUBLE_MAX = 1.7e308

#: 「不可比较」的哨兵值：**必须 ≤ 单精度上限**（否则写 `float` 列会被 1264 拒）
INCOMPARABLE_DIFF = 3e38

#: `on_inf` / `on_nan` 的合法策略
ON_SPECIAL_NONE = "none"        # 转 None（默认）
ON_SPECIAL_ZERO = "zero"        # 转 0（**谨慎**：会与真实 0 混淆）
ON_SPECIAL_CLAMP = "clamp"      # 夹到范围上界（哨兵语义）
#: 合法策略。**刻意没有 `keep`**：本模块的契约是「输出永远是有限 float 或 None」，
#: 允许保留 NaN/Inf 会破坏该契约（需要「极大值」语义时用 `clamp` 表达哨兵）。
VALID_SPECIAL_POLICIES: tuple[str, ...] = (
    ON_SPECIAL_NONE, ON_SPECIAL_ZERO, ON_SPECIAL_CLAMP)


def is_missing(value: Any) -> bool:
    """`None` 或 `NaN`（含 numpy / pandas 标量）→ True。

    实现要点（踩过才这么写）：
    - `np.float32(nan)` **不是** Python `float` 子类 → 不能只靠 `isinstance` 判断
    - `pd.NA != pd.NA` 返回 `pd.NA` 而非 bool → `bool()` 会抛 `TypeError`
      → 故 `!=` 判定要包 try，失败再退回「若 pandas 已加载则用它判」
    - 不主动 import pandas：核心工具不该为判缺失而拖入重依赖；
      但指标本来就来自 pandas，运行期它几乎总在 `sys.modules` 里
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    try:
        result = value != value            # NaN 的自反不等性
        if isinstance(result, bool):
            return result
        return bool(result)                # numpy.bool_
    except Exception:  # noqa: BLE001 - pd.NA / 数组 / 不可比较对象
        pass
    pandas = sys.modules.get("pandas")
    if pandas is not None:
        try:
            return bool(pandas.isna(value))
        except Exception:  # noqa: BLE001
            return False
    return False


def is_special(value: Any) -> bool:
    """`NaN` / `±Inf` → True。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isnan(f) or math.isinf(f)


def column_max(column: Any) -> float | None:
    """由 SQLAlchemy 列对象推断**该列的真实范围上界**（MySQL 侧）。

    - `Float`（无 `precision`）→ MySQL `float` 单精度 → `MYSQL_FLOAT_MAX`
    - `Float(precision=53)` / `Double` → `MYSQL_DOUBLE_MAX`
    - `Numeric` → 不做夹取（返回 `None`，由调用方决定）
    - 整数列 / 无法识别 → `None`

    ⚠️ 依据是「此项目的 MySQL 现实」：已验证真实库里 `mapped_column(Float)` 建出
    `float`（单精度）。**若某列真实列型与声明不符**（本项目就有 10 个这样的列），
    这里推断会偏保守（按单精度夹取）——保守方向是安全的。
    """
    typ = getattr(column, "type", column)
    if isinstance(typ, type):          # 传的是裸类型（如 `Float` 而非 `Float()`）
        try:
            typ = typ()
        except Exception:  # noqa: BLE001 - 需要参数的列型
            return None
    name = type(typ).__name__.lower()
    if name == "double":
        return MYSQL_DOUBLE_MAX
    if name == "float":
        precision = getattr(typ, "precision", None)
        if precision and int(precision) >= 53:
            return MYSQL_DOUBLE_MAX
        return MYSQL_FLOAT_MAX
    return None


def clamp_to_range(value: Any, *, max_abs: float = MYSQL_FLOAT_MAX) -> float:
    """把值夹到 `[-max_abs, max_abs]`；`NaN` → 0，`±Inf` → 对应极值。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f):
        return 0.0
    if math.isinf(f):
        return max_abs if f > 0 else -max_abs
    return max(-max_abs, min(max_abs, f))


def resolve_bound(*, column: Any = None, max_abs: float | None = None) -> float:
    """解析**有效范围上界**：显式 `max_abs` > 列型推断 > **保守默认（单精度）**。

    保守方向是安全的：按单精度处理意味着「即使真实列是 `float` 也一定能写进去」，
    代价是若真实列是 `double` 且值确实很大，会被夹小 —— 那种情况请显式传
    `column=`（本函数会按列型给出双精度上界）。
    """
    if max_abs is not None:
        return float(max_abs)
    if column is not None:
        bound = column_max(column)
        if bound is not None:
            return bound
    return MYSQL_FLOAT_MAX


def to_db_float(
    value: Any, *, column: Any = None, max_abs: float | None = None,
    on_nan: str = ON_SPECIAL_NONE, on_inf: str = ON_SPECIAL_NONE,
    on_out_of_range: str = ON_SPECIAL_CLAMP, default: float | None = None,
) -> float | None:
    """把任意值转成**可安全写入 MySQL 数值列**的 float（或 None）。

    **契约：除非法策略参数，本函数永不抛异常，输出永远是「有限 float 或 None」。**
    （归一化工具若会抛，调用方就得处处 try —— 那是把风险推给每个写入点。）

    Args:
        value: 待归一化的值（可为 numpy/pandas 标量）
        column: SQLAlchemy 列对象 → 按**真实列型**推断上界（推荐）
        max_abs: 显式上界（优先于列型推断）
        on_nan: `NaN` 策略（默认 `none`：项目语义里 NaN = 缺失/无效）
        on_inf: `±Inf` 策略（默认 `none`；哨兵语义的列传 `clamp`）
        on_out_of_range: 超上界策略（默认 `clamp`：夹到上界，保留「极大」语义）
        default: 非数值（如字符串）时的返回值（默认 None）

    Returns:
        `float` 或 `None` —— **永不返回 NaN/Inf**。

    范围口径（三条，避免臆测）：
        1. `abs(value) <= 有效上界` → 原样返回
        2. 无 `column`/`max_abs` 时，有效上界**按单精度**（`3.4028e38`）——
           保守方向安全：这样的值写任何 MySQL 数值列都不会被拒
        3. 真需要保留 >3.4e38 的值，请显式传 `column=`（列是 double 时按双精度）
    """
    for label, policy in (("on_nan", on_nan), ("on_inf", on_inf),
                          ("on_out_of_range", on_out_of_range)):
        if policy not in VALID_SPECIAL_POLICIES:
            raise ValueError(
                f"{label} 必须是 {list(VALID_SPECIAL_POLICIES)} 之一，收到 {policy!r}。")

    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default

    bound = resolve_bound(column=column, max_abs=max_abs)

    # ① NaN（把**原值**交给策略，`keep` 时才能原样保留）
    if math.isnan(f):
        return _apply_policy(on_nan, f, max_abs=bound, default=default)

    # ② ±Inf
    if math.isinf(f):
        return _apply_policy(on_inf, f, max_abs=bound, default=default)

    # ③ 超上界 → 按策略
    if abs(f) > bound:
        out = _apply_policy(on_out_of_range, f, max_abs=bound, default=default)
        # 兜底：任何策略下输出都必须有限（`keep` 也不许把 1e400 放出去）
        if out is None:
            return None
        out = float(out)
        if not math.isfinite(out) or abs(out) > MYSQL_DOUBLE_MAX:
            return None
        return out

    # ④ 落在范围内（含 `keep` 的常态路径）
    if abs(f) > MYSQL_DOUBLE_MAX:      # 连双精度都放不下
        return None
    return f


def _apply_policy(policy: str, value: float, *, max_abs: float,
                  default: float | None) -> float | None:
    """把特殊值/超界值按策略映射为「可写」结果。"""
    if policy == ON_SPECIAL_NONE:
        return None
    if policy == ON_SPECIAL_ZERO:
        return 0.0
    # 只剩 clamp（`keep` 已移除：它会让 NaN/Inf 泄漏出本模块）
    return clamp_to_range(value, max_abs=max_abs)


def to_float_or_none(value: Any, *, column: Any = None) -> float | None:
    """最常用语义：**NaN/Inf → None**，超范围按列型夹取。

    等价于 `to_db_float(value, column=column)`；单独命名是为了让调用点自解释。
    """
    return to_db_float(value, column=column)


def clean_numeric_fields(
    payload: Mapping[str, Any] | None, *,
    columns: Mapping[str, Any] | None = None,
    on_inf: str = ON_SPECIAL_NONE,
    on_out_of_range: str = ON_SPECIAL_NONE,
) -> dict[str, Any]:
    """把一个 dict 里的数值字段清洗成「可写库」形态（**只动数值，不动字符串/None**）。

    Args:
        payload: 待清洗的字段字典（如指标 dict）
        columns: 可选的「字段名 → SQLAlchemy 列对象」映射（用于按列型夹取）
        on_inf: `±Inf` 策略（默认 `none`）

    Returns:
        新 dict（不改原对象）；**不会出现 NaN/Inf**。
    """
    out: dict[str, Any] = {}
    src = dict(payload or {})
    col_map = dict(columns or {})
    for key, value in src.items():
        if value is None or isinstance(value, (str, bytes, bool)):
            out[key] = value
            continue
        if is_special(value) or (
            isinstance(value, (int, float)) and abs(float(value)) > MYSQL_FLOAT_MAX
        ):
            # 批量清洗**不该抛异常**：无列型信息时把超界值视为「不可用」（None）
            out[key] = to_db_float(value, column=col_map.get(key), on_inf=on_inf,
                                   on_out_of_range=on_out_of_range)
            continue
        # numpy / pandas 标量 → Python 原生（避免驱动层再遇到特殊类型）
        if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
            try:
                out[key] = value.item()
                continue
            except Exception:  # noqa: BLE001
                pass
        out[key] = value
    return out


def clean_json_tree(value: Any) -> Any:
    """递归清洗任意「JSON 形」值树 → 可安全 `json.dumps(..., allow_nan=False)`。

    为什么独立于 `clean_numeric_fields`（TD3 实测）：后者只处理**顶层标量**，而
    `metrics_json` 有**嵌套结构**（如 wp5_eval_task 的 `stress_test` 子 dict，
    其 `ic_decay_ratio` 可为 NaN）；`json.dumps(allow_nan=False)` 对嵌套里的
    NaN/±Inf 同样抛 ValueError —— 递归清洗才能覆盖整棵树。

    规则（口径与 D-F/R33 及 `clean_numeric_fields` 默认一致）：
    - `None` / `str` / `bytes` / `bool`：原样（**bool 是 int 子类，必须先判**）
    - `dict`：key 原样，value 递归；`list` / `tuple`：逐元素递归（tuple → list，
      与 `json.dumps` 自身行为一致；`set` **不处理**——dumps 本就不支持，保持原样）
    - 数值（含 numpy / pandas 标量，经 `.item()` 归一）：`NaN` / `±Inf` /
      超出 `MYSQL_FLOAT_MAX` → `None`（**绝不归 0**；超界转 None 与
      `clean_numeric_fields` 默认 `on_out_of_range=none` 对齐）
    - 其他对象（date/Decimal/自定义）：原样返回，由调用方 `json.dumps(default=...)`
      兜底 —— 本函数**只修 NaN/±Inf，不改变其他序列化语义**

    TD3 五个 `metrics_json` 写入点统一走：
        json.dumps(clean_json_tree(metrics), ..., allow_nan=False)
    """
    if value is None or isinstance(value, (str, bytes, bool)):
        return value
    if isinstance(value, dict):
        return {k: clean_json_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json_tree(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        try:
            return clean_json_tree(value.item())
        except Exception:  # noqa: BLE001 - item() 失败则按普通对象继续
            pass
    if is_special(value):
        return None
    if isinstance(value, int):
        # bool 已在入口原样返回；int 是 JSON 原生类型（任意精度）——
        # **不转 float**：转 float 会把 7 变 7.0（类型改变）且超大 int 会溢出
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) and abs(value) <= MYSQL_FLOAT_MAX else None
    return value


__all__ = [
    "MYSQL_FLOAT_MAX",
    "MYSQL_DOUBLE_MAX",
    "INCOMPARABLE_DIFF",
    "ON_SPECIAL_NONE",
    "ON_SPECIAL_ZERO",
    "ON_SPECIAL_CLAMP",
    "VALID_SPECIAL_POLICIES",
    "resolve_bound",
    "is_missing",
    "is_special",
    "column_max",
    "clamp_to_range",
    "to_db_float",
    "to_float_or_none",
    "clean_numeric_fields",
    "clean_json_tree",
]
