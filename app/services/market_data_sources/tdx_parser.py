"""通达信(TDX)本地二进制文件解析器。

借鉴 EasyXT 利用通达信本地文件的思路:通达信客户端免费,行情数据下载到本地 .day 二进制文件,
读本地文件零网络、零频率限制、零成本,适合作为 akshare 风控触发时的兜底源。

文件格式(每条记录 32 字节,大端序):
    int32  date      日期 YYYYMMDD
    int32  open      开盘价(分,需 /100)
    float32 high     最高价
    float32 low      最低价
    float32 close    收盘价
    float32 amount   成交额(元)
    int32  volume    成交量(手)
    int32  reserved  保留字段

文件路径:
    vipdoc/sh/lday/sh600000.day  ← 上交所
    vipdoc/sz/lday/sz000001.day  ← 深交所
    vipdoc/sh/fzline/sh600000.lc5  ← 5分钟线(暂不解析)
    vipdoc/sh/fzline/sh600000.lc1  ← 1分钟线(暂不解析)

注意:
- 通达信 open 字段存的是"分",需 /100 转为"元"
- 通达信不提供复权因子,返回不复权价
- 通达信不提供 turnover_rate,该列留空
"""
from __future__ import annotations

import logging
import os
import struct
from datetime import date, datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# 每条记录 32 字节,大端序
# > 表示大端序(big-endian)
# i = int32, f = float32
# 字段顺序:date, open, high, low, close, amount, volume, reserved
RECORD_FORMAT = ">iiffffii"
RECORD_SIZE = struct.calcsize(RECORD_FORMAT)  # = 32 字节

# 标准化后的列名(与 _standard_history_frame 对齐)
COLUMNS = ["trade_date", "open", "high", "low", "close", "amount", "volume"]


def parse_day_file(
    filepath: Path,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """解析 .day 文件,返回标准化 DataFrame。

    Args:
        filepath: .day 文件路径
        start: 起始日期(可选,None 表示不限)
        end: 结束日期(可选,None 表示不限)

    Returns:
        DataFrame,列:trade_date, open, high, low, close, amount, volume
        文件不存在或为空时返回空 DataFrame(不抛异常,由上层 source 判断是否降级)

    Raises:
        无主动抛出,文件 IO 异常由上层捕获并转 SourceUnavailable
    """
    if not filepath.exists():
        return pd.DataFrame(columns=COLUMNS)

    start_int = int(start.strftime("%Y%m%d")) if start else 0
    end_int = int(end.strftime("%Y%m%d")) if end else 99991231

    records: list[dict] = []
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(RECORD_SIZE)
            if len(chunk) < RECORD_SIZE:
                break
            date_int, open_p, high, low, close, amount, volume, _ = struct.unpack(RECORD_FORMAT, chunk)
            # 日期过滤(在解析阶段过滤,减少内存占用)
            if date_int < start_int or date_int > end_int:
                continue
            records.append({
                "trade_date": datetime.strptime(str(date_int), "%Y%m%d").date(),
                "open": open_p / 100.0,   # 通达信开盘价存的是"分",需 /100 转为"元"
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "amount": float(amount),
                "volume": int(volume),
            })

    if not records:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(records)
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def detect_tdx_path() -> str | None:
    """自动检测通达信安装路径。

    检查顺序:
        1. 环境变量 TDX_PATH(用户显式配置,优先级最高)
        2. Windows 注册表 HKLM\\SOFTWARE\\TDX\\InstallDir
        3. 常见安装路径扫描

    Returns:
        通达信安装路径(含 vipdoc 目录),未检测到返回 None
    """
    # 1. 环境变量优先
    env_path = os.getenv("TDX_PATH")
    if env_path and Path(env_path).exists() and (Path(env_path) / "vipdoc").exists():
        return env_path
    if env_path and Path(env_path).exists():
        logger.debug("TDX_PATH=%s exists but vipdoc/ not found", env_path)

    # 2. Windows 注册表检测
    if os.name == "nt":
        try:
            import winreg
            for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    key = winreg.OpenKey(hive, r"SOFTWARE\TDX")
                    install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
                    winreg.CloseKey(key)
                    if install_dir and Path(install_dir).exists() and (Path(install_dir) / "vipdoc").exists():
                        return install_dir
                except OSError:
                    continue
        except ImportError:
            pass

    # 3. 常见路径扫描
    common_paths = [
        r"C:\new_tdx",
        r"D:\new_tdx",
        r"C:\通达信",
        r"D:\通达信",
        r"C:\Program Files\通达信",
        r"D:\Program Files\通达信",
        r"C:\new_tdx\通达信金融终端",
        r"D:\new_tdx\通达信金融终端",
    ]
    for p in common_paths:
        if Path(p).exists() and (Path(p) / "vipdoc").exists():
            return p

    return None


def symbol_to_tdx_code(symbol) -> str:
    """Symbol 模型转通达信文件名前缀。

    Args:
        symbol: Symbol 模型实例,需有 symbol 和 market 字段

    Returns:
        通达信代码(如 "sz000001" / "sh600000")

    Examples:
        Symbol(symbol="000001", market="SZ") → "sz000001"
        Symbol(symbol="600000", market="SH") → "sh600000"
    """
    market = (symbol.market or "").lower()
    if market in {"sh", "cn"} or symbol.symbol.startswith(("5", "6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    return f"{prefix}{symbol.symbol}"


def tdx_file_path(tdx_path: str, symbol) -> Path:
    """根据 Symbol 构造 .day 文件路径。

    Args:
        tdx_path: 通达信安装根目录
        symbol: Symbol 模型实例

    Returns:
        .day 文件 Path 对象

    Examples:
        tdx_path="C:\\new_tdx", Symbol(symbol="000001", market="SZ")
        → Path("C:\\new_tdx\\vipdoc\\sz\\lday\\sz000001.day")
    """
    code = symbol_to_tdx_code(symbol)
    prefix = code[:2]  # sh / sz
    return Path(tdx_path) / "vipdoc" / prefix / "lday" / f"{code}.day"
