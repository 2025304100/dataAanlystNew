"""候选池 · 外部导入与逐行匹配（向导 §3.1 方式 A / SD-v2.0 §6.3；任务 T10）。

职责边界
========
✅ 解析 CSV / Excel 导入文件（`symbol` / `name` / `market` / `note`）
✅ 按 `symbols` 主数据逐行匹配，产出**六种行状态**的逐行结果
✅ 只有「成功」行入池；其余一律**不入池**并给出可下载的错误明细
✅ 生成导入模板（CSV / xlsx）

❌ 不做：条件筛选（→ `rules.py`）／成员 CRUD（→ `service.py`）／快照看板（→ T11）

隔离模型（向导 §3.1 / 需求 §3.0.1）
=================================
- **股票代码是唯一匹配键**。导入文件里的 `name` / `market` **只用于校验提示**，
  与系统主数据不一致时只记 warning，**绝不回写** `symbols`。
- 导入方式**不提供**二次筛选控件（板块 / PE / ST 之类）——
  需要按条件筛选请走「方式 B」（`rules.py`）。

逐行结果：六种状态（**与向导 §3.1 逐字对应**）
==========================================
| 状态 | 含义 | 是否入池 |
|---|---|---|
| `success` | 匹配到 A 股 stock 且可交易 | ✅ |
| `unmatched` | 代码在 `symbols` 中不存在 | ❌ |
| `duplicate` | 同一代码在**文件内**出现多次 | ❌（仅首次有效） |
| `format_error` | 空值或非 6 位数字 | ❌ |
| `non_stock` | 匹配到但 `asset_type != 'stock'`（如 ETF） | ❌ |
| `suspended` | 匹配到但 `is_active = 0` | ❌ |

⚠️ 关于「已停牌」的实测口径（2026-09-16）
----------------------------------------
向导要求的 `suspended` 一类，**本项目没有真正的停牌字段**（无 `security_status_daily`
可用数据 —— 该表全部是 e2e fixture，详见 T09 observations）。

实测 `symbols.is_active`：1 = 7,272 行、0 = 367 行；
A 股 stock 中 `is_active=0` 只有 **7 只**，名称全部是「退市XX」。
即 `is_active=0` 的真实语义是**已退市 / 不可交易**，不是停牌。

处理方式：用 `is_active=0` 承载向导的 `suspended` 一类，但在每一行的
`reason_zh` 里**写明真实含义**，不把它包装成停牌 —— 否则用户会以为系统能识别停牌。

关于「前导零丢失」
------------------
Excel 直接打开 CSV 会把 `000001` 变成 `1`。本模块**不自动补零**：
补零会静默改变用户意图（`1` 也可能真的是无效输入）。
改为在 `format_error` 的 `reason_zh` 里给出可操作的提示
（"若为 Excel 打开导致前导零丢失，请将该列设为文本格式后重新保存"）。
这与 T05 处理 `delta` 单位歧义时「判不出就报错、不猜」是同一条原则。
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.symbol import Symbol
from app.schemas.errors import FactorSevenError
from app.services.factors.candidate_pool import service as pool_service

# ══════════════════════════════════════════════════════════════
# 常量
# ═════════════════════════════════════════════════════════════=

#: 模板列（向导 §3.1：`symbol` 必填，其余可选）
IMPORT_COLUMNS: tuple[str, ...] = ("symbol", "name", "market", "note")
REQUIRED_COLUMN = "symbol"
OPTIONAL_COLUMNS: tuple[str, ...] = ("name", "market", "note")

#: 支持的格式
FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"
FORMAT_XLS = "xls"
SUPPORTED_FORMATS: tuple[str, ...] = (FORMAT_CSV, FORMAT_XLSX, FORMAT_XLS)

#: A 股代码格式（实测：A 股 stock 全部为 6 位数字）
SYMBOL_PATTERN = re.compile(r"^\d{6}$")

#: 单次导入的行数上限（防御性；UI 单页选择量远小于此）
MAX_IMPORT_ROWS = 20_000

#: 单次读库匹配的 IN 分批大小（避免超长 IN 列表）
MATCH_BATCH = 1000

#: 写成员的分批大小（`service.add_members` 服务级上限 5000，这里更保守）
WRITE_BATCH = 1000

#: CSV 编码尝试顺序（中文 Excel 导出的 CSV 常见 GBK）
CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "gbk", "utf-8")

#: 市场列的可选值提示（仅用于提示，不回写）
KNOWN_MARKETS: tuple[str, ...] = ("sh", "sz", "bj")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════════
# 错误工厂
#
# 刻意**不复用** `service._validation_error` —— 那是别模块的私有实现，
# 跨模块依赖私有函数会在对方重构时静默失效。这里按同一份 7 要素契约
# 各自实现一份（契约在 `app/schemas/errors.py`，不在此处）。
# ══════════════════════════════════════════════════════════════


def _validation_error(detail: str, **extras: Any) -> FactorSevenError:
    return FactorSevenError(
        "VALIDATION_ERROR",
        detail_zh=detail,
        impact="本次导入未执行",
        fix_link="/settings/factor-mining?step=1",
        retryable=False,
        extras=extras or None,
    )


def _business_blocked(detail: str, *, reason: str, **extras: Any) -> FactorSevenError:
    return FactorSevenError(
        "BUSINESS_BLOCKED",
        title_zh="当前状态不允许该操作",
        detail_zh=detail,
        impact="本次导入被拒绝，数据未发生变更",
        fix_link="/settings/factor-mining?step=1",
        retryable=False,
        extras={"reason": reason, **extras},
    )


# ══════════════════════════════════════════════════════════════
# 行状态
# ══════════════════════════════════════════════════════════════


class RowStatus:
    """逐行匹配结果的状态码。"""

    SUCCESS = "success"
    UNMATCHED = "unmatched"
    DUPLICATE = "duplicate"
    FORMAT_ERROR = "format_error"
    NON_STOCK = "non_stock"
    SUSPENDED = "suspended"


#: 会入池的状态（其余全部不入池）
ADMITTED_STATUSES: frozenset[str] = frozenset({RowStatus.SUCCESS})

STATUS_LABELS_ZH: dict[str, str] = {
    RowStatus.SUCCESS: "成功",
    RowStatus.UNMATCHED: "未匹配",
    RowStatus.DUPLICATE: "文件内重复",
    RowStatus.FORMAT_ERROR: "格式错误",
    RowStatus.NON_STOCK: "非股票资产",
    RowStatus.SUSPENDED: "已停牌/不可交易",
}

#: 错误明细里要包含的状态（成功行不需要出现在错误文件中）
ERROR_STATUSES: tuple[str, ...] = (
    RowStatus.UNMATCHED,
    RowStatus.DUPLICATE,
    RowStatus.FORMAT_ERROR,
    RowStatus.NON_STOCK,
    RowStatus.SUSPENDED,
)


# ══════════════════════════════════════════════════════════════
# 输出类型
# ══════════════════════════════════════════════════════════════


@dataclass
class ImportRow:
    """导入文件中的一行（`row_no` 从 **2** 起，与 Excel 行号一致）。"""

    row_no: int
    symbol_raw: str
    name: str | None = None
    market: str | None = None
    note: str | None = None


@dataclass
class RowResult:
    """逐行匹配结果。**只有 `success` 会入池**。"""

    row_no: int
    status: str
    symbol_raw: str
    symbol: str | None = None
    symbol_id: int | None = None
    name_in_file: str | None = None
    name_in_system: str | None = None
    market_in_file: str | None = None
    market_in_system: str | None = None
    note: str | None = None
    reason_zh: str = ""
    #: 名称/市场与主数据不一致的提示（**不回写主数据**，只提示）
    warnings: list[str] = field(default_factory=list)

    @property
    def admitted(self) -> bool:
        return self.status in ADMITTED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no,
            "status": self.status,
            "status_label_zh": STATUS_LABELS_ZH.get(self.status, self.status),
            "admitted": self.admitted,
            "symbol_raw": self.symbol_raw,
            "symbol": self.symbol,
            "symbol_id": self.symbol_id,
            "name_in_file": self.name_in_file,
            "name_in_system": self.name_in_system,
            "market_in_file": self.market_in_file,
            "market_in_system": self.market_in_system,
            "note": self.note,
            "reason_zh": self.reason_zh,
            "warnings": list(self.warnings),
        }


@dataclass
class ImportResult:
    """一次导入的完整结果。"""

    pool_id: str | None
    import_batch_id: str
    total_rows: int
    rows: list[RowResult] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    #: 写成员的结果（由 `service.add_members` 汇总而来）
    member_write: dict[str, Any] = field(default_factory=dict)
    committed: bool = False
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())

    @property
    def admitted_count(self) -> int:
        return self.counts.get(RowStatus.SUCCESS, 0)

    @property
    def error_count(self) -> int:
        return self.total_rows - self.admitted_count

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "import_batch_id": self.import_batch_id,
            "total_rows": self.total_rows,
            "admitted_count": self.admitted_count,
            "error_count": self.error_count,
            "has_errors": self.has_errors,
            "counts": dict(self.counts),
            "counts_label_zh": {k: STATUS_LABELS_ZH.get(k, k) for k in self.counts},
            "member_write": dict(self.member_write),
            "committed": self.committed,
            "created_at": self.created_at,
            "rows": [r.to_dict() for r in self.rows],
        }


# ══════════════════════════════════════════════════════════════
# 文件解析
# ══════════════════════════════════════════════════════════════


def detect_format(filename: str | None, declared: str | None = None) -> str:
    """按扩展名判定格式。声明优先，其次文件名后缀。"""
    cand = (declared or "").strip().lower().lstrip(".")
    if cand in SUPPORTED_FORMATS:
        return cand
    name = (filename or "").strip().lower()
    for fmt in (FORMAT_XLSX, FORMAT_XLS, FORMAT_CSV):
        if name.endswith("." + fmt):
            return fmt
    raise _validation_error(
        f"无法判定导入文件格式。支持 {list(SUPPORTED_FORMATS)}"
        f"（文件名 {filename!r} 未带可识别后缀）。",
        allowed=list(SUPPORTED_FORMATS),
    )


def _read_csv(file_bytes: bytes) -> pd.DataFrame:
    """读 CSV：按 `utf-8-sig → gbk → utf-8` 依次尝试编码。

    中文 Excel 导出的 CSV 常见 GBK；只试 utf-8 会直接抛 `UnicodeDecodeError`，
    用户完全不知道发生了什么，故这里显式兜底并在失败时给出可读的报错。
    """
    last_err: Exception | None = None
    for enc in CSV_ENCODINGS:
        try:
            return pd.read_csv(io.BytesIO(file_bytes), dtype=str, encoding=enc,
                               keep_default_na=False)
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    raise _validation_error(
        f"CSV 文件编码无法识别（已尝试 {list(CSV_ENCODINGS)}）。"
        "请将文件另存为 UTF-8 或 GBK 编码后重试。",
    ) from last_err


def _read_excel(file_bytes: bytes, fmt: str) -> pd.DataFrame:
    """读 Excel。`dtype=str` 是**必需的** —— 否则 pandas 会把 `000001` 读成整数 1。"""
    engine = "xlrd" if fmt == FORMAT_XLS else "openpyxl"
    try:
        return pd.read_excel(io.BytesIO(file_bytes), dtype=str, engine=engine,
                             keep_default_na=False)
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise _validation_error(
            f"读取 {fmt} 需要 {engine}，当前环境未安装。请改用 CSV 或安装依赖。",
        ) from exc
    except Exception as exc:
        raise _validation_error(
            f"{fmt} 文件解析失败：{exc}。若文件受密码保护或已损坏，请另存为 CSV 后重试。",
        ) from exc


def _normalize_header(name: Any) -> str:
    return str(name or "").strip().lower().replace(" ", "").replace("_", "")


def parse_import_file(file_bytes: bytes, *, filename: str | None = None,
                      fmt: str | None = None) -> list[ImportRow]:
    """解析导入文件 → 逐行 `ImportRow`。

    - 列名大小写 / 空格 / 下划线不敏感
    - 缺失 `symbol` 列 → 直接报错（而不是静默产出 0 行）
    - 未知列忽略（用户常带额外列）
    - 空行跳过
    """
    if not file_bytes:
        raise _validation_error("导入文件为空。")
    detected = detect_format(filename, fmt)

    if detected == FORMAT_CSV:
        frame = _read_csv(file_bytes)
    else:
        frame = _read_excel(file_bytes, detected)

    if frame is None or frame.empty:
        return []

    col_map = {_normalize_header(c): c for c in frame.columns}

    def _first_key(*keys: str) -> Any:
        """返回第一个命中的**列名**（未命中返回 None）。

        ⚠️ 不能写 `col_map.get(a) or col_map.get(b)`：命中时返回的是 pandas Series，
        `Series or ...` 会触发 `__nonzero__` → `ValueError: The truth value of a
        Series is ambiguous`。
        """
        for k in keys:
            if k in col_map:
                return col_map[k]
        return None

    sym_col = _first_key("symbol", "code", "代码", "股票代码")
    if sym_col is None:
        raise _validation_error(
            f"导入文件缺少 `symbol` 列（股票代码，必填）。"
            f"当前识别到的列：{[str(c) for c in frame.columns]}。"
            "请先下载导入模板，按模板列填写。",
            found_columns=[str(c) for c in frame.columns],
            expected_columns=list(IMPORT_COLUMNS),
        )

    def _series_or_none(*keys: str) -> Any:
        """返回第一个命中的列 Series（未命中返回 None）。同上：不能用 `or` 串联。"""
        key = _first_key(*keys)
        return frame[key] if key is not None else None

    name_col = _series_or_none("name", "名称", "股票名称")
    market_col = _series_or_none("market", "市场")
    note_col = _series_or_none("note", "备注")

    total = len(frame)
    if total > MAX_IMPORT_ROWS:
        raise _validation_error(
            f"导入文件共 {total} 行，超过单次上限 {MAX_IMPORT_ROWS} 行。请拆分后分批导入。",
            total_rows=total,
            max_rows=MAX_IMPORT_ROWS,
        )

    rows: list[ImportRow] = []
    for i in range(total):
        raw_sym = frame.iloc[i][sym_col]
        raw_name = name_col.iloc[i] if name_col is not None else None
        raw_market = market_col.iloc[i] if market_col is not None else None
        raw_note = note_col.iloc[i] if note_col is not None else None

        sym = "" if raw_sym is None else str(raw_sym).strip()
        name = None if raw_name is None or str(raw_name).strip() == "" \
            else str(raw_name).strip()
        market = None if raw_market is None or str(raw_market).strip() == "" \
            else str(raw_market).strip().lower()
        note = None if raw_note is None or str(raw_note).strip() == "" \
            else str(raw_note).strip()

        # 整行全空 → 跳过（Excel 常见尾部空行）
        if not sym and name is None and market is None and note is None:
            continue
        rows.append(ImportRow(row_no=i + 2, symbol_raw=sym, name=name,
                              market=market, note=note))
    return rows


# ══════════════════════════════════════════════════════════════
# 逐行匹配
# ══════════════════════════════════════════════════════════════


def _format_error_reason(raw: str) -> str:
    if not raw:
        return "股票代码为空。"
    if raw.isdigit() and len(raw) < 6:
        return (
            f"股票代码 {raw!r} 不是 6 位。若为 Excel 打开导致**前导零丢失**，"
            "请将该列设为「文本」格式后重新保存（例如 1 → 000001）。"
        )
    return f"股票代码 {raw!r} 不是 6 位数字（A 股代码格式）。"


def match_rows(db: Session, rows: Sequence[ImportRow]) -> list[RowResult]:
    """把导入行与 `symbols` 主数据逐行匹配。**不写任何东西**。"""
    results: list[RowResult] = []
    if not rows:
        return results

    # ① 格式校验 + 文件内重复（都在 Python 侧，不查库）
    seen: dict[str, int] = {}
    valid_codes: list[str] = []
    for row in rows:
        raw = row.symbol_raw
        if not SYMBOL_PATTERN.match(raw):
            results.append(RowResult(
                row_no=row.row_no, status=RowStatus.FORMAT_ERROR, symbol_raw=raw,
                name_in_file=row.name, market_in_file=row.market, note=row.note,
                reason_zh=_format_error_reason(raw),
            ))
            continue
        if raw in seen:
            results.append(RowResult(
                row_no=row.row_no, status=RowStatus.DUPLICATE, symbol_raw=raw,
                symbol=raw, name_in_file=row.name, market_in_file=row.market,
                note=row.note,
                reason_zh=f"该代码已在第 {seen[raw]} 行出现过，本行忽略（保留首次）。",
            ))
            continue
        seen[raw] = row.row_no
        valid_codes.append(raw)

    # ② 批量查主数据
    meta: dict[str, Symbol] = {}
    for i in range(0, len(valid_codes), MATCH_BATCH):
        chunk = valid_codes[i:i + MATCH_BATCH]
        for s in db.execute(
            select(Symbol).where(Symbol.symbol.in_(chunk))
        ).scalars():
            meta[str(s.symbol)] = s

    # ③ 逐行判定
    for row in rows:
        raw = row.symbol_raw
        if not SYMBOL_PATTERN.match(raw) or seen.get(raw) != row.row_no:
            continue  # 已在 ① 处理
        sym = meta.get(raw)
        if sym is None:
            results.append(RowResult(
                row_no=row.row_no, status=RowStatus.UNMATCHED, symbol_raw=raw,
                symbol=raw, name_in_file=row.name, market_in_file=row.market,
                note=row.note,
                reason_zh=f"代码 {raw} 在 symbols 主数据中不存在，未匹配。",
            ))
            continue

        warnings: list[str] = []
        if row.name and sym.name and row.name != str(sym.name):
            warnings.append(
                f"文件中的名称 {row.name!r} 与系统主数据 {sym.name!r} 不一致 —— "
                "**以系统主数据为准，不回写**。"
            )
        # ⚠️ 两条检查**独立**（不能用 elif）：市场值既可能「与主数据不一致」
        #    又可能「本身不是已知值」，用 elif 会让前一条把后一条吞掉。
        if row.market and sym.market and row.market.lower() != str(sym.market).lower():
            warnings.append(
                f"文件中的市场 {row.market!r} 与系统主数据 {sym.market!r} 不一致 —— "
                "**以系统主数据为准，不回写**（匹配以代码为准，市场不参与）。"
            )
        if row.market and row.market not in KNOWN_MARKETS:
            warnings.append(
                f"文件中的市场 {row.market!r} 不是已知值 {list(KNOWN_MARKETS)}，"
                "仅作为记录保存，未用于匹配。"
            )

        if str(sym.asset_type) != "stock":
            results.append(RowResult(
                row_no=row.row_no, status=RowStatus.NON_STOCK, symbol_raw=raw,
                symbol=str(sym.symbol), symbol_id=int(sym.id),
                name_in_file=row.name, name_in_system=_s(sym.name),
                market_in_file=row.market, market_in_system=_s(sym.market),
                note=row.note, warnings=warnings,
                reason_zh=(
                    f"代码 {raw} 的资产类型是 {sym.asset_type}（非股票），"
                    "候选池只支持股票资产。"
                ),
            ))
            continue

        if int(getattr(sym, "is_active", 1) or 0) == 0:
            results.append(RowResult(
                row_no=row.row_no, status=RowStatus.SUSPENDED, symbol_raw=raw,
                symbol=str(sym.symbol), symbol_id=int(sym.id),
                name_in_file=row.name, name_in_system=_s(sym.name),
                market_in_file=row.market, market_in_system=_s(sym.market),
                note=row.note, warnings=warnings,
                reason_zh=(
                    f"代码 {raw} 在主数据中 `is_active=0`（已退市 / 不可交易），不入池。"
                    "注：本项目**无真正的停牌字段**（`security_status_daily` 仅为 e2e "
                    "fixture 数据），该状态实际表达的是「已退市/不可交易」。"
                ),
            ))
            continue

        results.append(RowResult(
            row_no=row.row_no, status=RowStatus.SUCCESS, symbol_raw=raw,
            symbol=str(sym.symbol), symbol_id=int(sym.id),
            name_in_file=row.name, name_in_system=_s(sym.name),
            market_in_file=row.market, market_in_system=_s(sym.market),
            note=row.note, warnings=warnings, reason_zh="匹配成功。",
        ))

    results.sort(key=lambda r: r.row_no)
    return results


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def _count_by_status(rows: Iterable[RowResult]) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in STATUS_LABELS_ZH}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {k: v for k, v in counts.items() if v}


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════


def import_members(db: Session, *, pool_id: str | None, file_bytes: bytes,
                   filename: str | None = None, fmt: str | None = None,
                   operator_id: str = "system",
                   import_batch_id: str | None = None,
                   commit: bool = True) -> ImportResult:
    """解析 → 逐行匹配 →（可选）把成功行写入候选池。

    `commit=False` 时为**预览**：只返回逐行结果，不写成员（向导 §3.5 的
    「导入方式显示逐行匹配结果」—— 用户先看结果再决定要不要入池）。

    写入采用分批（`WRITE_BATCH`）并汇总 `service.add_members` 的统计；
    若某批失败，会在异常 `extras` 里带上已完成批次数与 `pool_id`，
    让用户能续做而不是面对一个「不知道写到哪了」的半截状态。
    """
    pool = pool_service.get_pool(db, pool_id) if (pool_id and commit) else None
    if commit and pool is not None and pool.source_type != pool_service.SOURCE_TYPE_IMPORT:
        raise _business_blocked(
            f"候选池 {pool_id} 的 source_type 是 {pool.source_type}，"
            "只有 `import` 类型的池可以导入成员（两种方式互斥，创建后不可切换）。",
            reason="SOURCE_TYPE_IMMUTABLE",
            pool_id=pool_id,
            source_type=pool.source_type,
        )

    rows = parse_import_file(file_bytes, filename=filename, fmt=fmt)
    results = match_rows(db, rows)
    batch_id = import_batch_id or uuid.uuid4().hex

    result = ImportResult(
        pool_id=pool_id,
        import_batch_id=batch_id,
        total_rows=len(results),
        rows=results,
        counts=_count_by_status(results),
    )

    if not commit:
        return result

    if pool is None:
        return result

    # ⚠️ 不在这里显式调 `service._assert_not_locked`（私有函数，跨模块依赖会在对方
    #    重构时静默失效）。锁定态由 `service.add_members` 内部保证 —— 它每次都会调。
    #    若所有行都未匹配成功，`symbol_ids` 为空、循环不执行，也就不存在写入。
    symbol_ids = [r.symbol_id for r in results
                  if r.admitted and r.symbol_id is not None]
    # 保序去重（`service.add_members` 也会去重，但这里先做可减少无效传输）
    seen: set[int] = set()
    ordered = [i for i in symbol_ids if not (i in seen or seen.add(i))]

    agg = pool_service.MemberChangeResult(pool_id=pool_id, requested=len(ordered),
                                          member_count=0)
    done_batches = 0
    chunks = [ordered[i:i + WRITE_BATCH] for i in range(0, len(ordered), WRITE_BATCH)]
    try:
        for chunk in chunks:
            part = pool_service.add_members(
                db, pool_id, symbol_ids=chunk, operator_id=operator_id,
                source=pool_service.SOURCE_TYPE_IMPORT,
            )
            agg.added += part.added
            agg.reactivated += part.reactivated
            agg.skipped_existing += part.skipped_existing
            agg.member_count = part.member_count
            done_batches += 1
    except FactorSevenError as exc:
        exc.extras = {**(exc.extras or {}),
                      "import_batch_id": batch_id,
                      "completed_batches": done_batches,
                      "total_batches": len(chunks)}
        raise

    result.member_write = {
        "requested": len(ordered),
        "batches": len(chunks),
        **agg.to_dict(),
    }
    result.committed = True
    return result


# ══════════════════════════════════════════════════════════════
# 模板与错误明细
# ══════════════════════════════════════════════════════════════

_TEMPLATE_SAMPLE: tuple[tuple[str, ...], ...] = (
    ("000001", "", "", "示例：平安银行"),
    ("600519", "", "", "示例：贵州茅台"),
)


def build_template(fmt: str = FORMAT_CSV) -> tuple[bytes, str, str]:
    """生成导入模板。返回 `(bytes, filename, media_type)`。"""
    detected = detect_format(None, fmt)
    if detected == FORMAT_CSV:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(IMPORT_COLUMNS)
        for row in _TEMPLATE_SAMPLE:
            writer.writerow(row)
        # BOM 让 Excel 以 UTF-8 打开，避免中文乱码
        payload = buf.getvalue().encode("utf-8-sig")
        return payload, "candidate_pool_template.csv", "text/csv; charset=utf-8"

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "候选池导入"
    ws.append(list(IMPORT_COLUMNS))
    for row in _TEMPLATE_SAMPLE:
        ws.append(list(row))
    # 把 symbol 列强制设为文本，防止 Excel 吃掉前导零
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    ws.column_dimensions[get_column_letter(1)].number_format = "@"
    for cell in ws["A"]:
        cell.alignment = Alignment(horizontal="left")
    out = io.BytesIO()
    wb.save(out)
    return (out.getvalue(), "candidate_pool_template.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def build_error_csv(result: ImportResult) -> bytes:
    """把**未入池**的行导出为 CSV（向导 §3.1：「用户可下载错误明细，修正后重新导入」）。

    设计取向：错误明细**回写用户原始数据**（`symbol_raw`、文件里的名称/市场/备注），
    并附上行号与原因 —— 用户可以直接在下载的文件上改完重新导入，
    不需要自己把原因对应回原文件。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["row_no", "status", "status_label_zh", "symbol",
                     "name_in_file", "market_in_file", "note", "reason_zh"])
    for r in result.rows:
        if r.status in ADMITTED_STATUSES:
            continue
        writer.writerow([
            r.row_no, r.status, STATUS_LABELS_ZH.get(r.status, r.status),
            r.symbol_raw, r.name_in_file or "", r.market_in_file or "",
            r.note or "", r.reason_zh,
        ])
    return buf.getvalue().encode("utf-8-sig")


__all__ = [
    "IMPORT_COLUMNS",
    "REQUIRED_COLUMN",
    "OPTIONAL_COLUMNS",
    "SUPPORTED_FORMATS",
    "FORMAT_CSV",
    "FORMAT_XLSX",
    "FORMAT_XLS",
    "MAX_IMPORT_ROWS",
    "SYMBOL_PATTERN",
    "RowStatus",
    "ADMITTED_STATUSES",
    "STATUS_LABELS_ZH",
    "ERROR_STATUSES",
    "ImportRow",
    "RowResult",
    "ImportResult",
    "detect_format",
    "parse_import_file",
    "match_rows",
    "import_members",
    "build_template",
    "build_error_csv",
    "_count_by_status",
]
