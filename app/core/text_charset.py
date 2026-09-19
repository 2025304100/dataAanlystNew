# -*- coding: utf-8 -*-
"""全库 utf8mb3 写入守卫（TD6 / C1 裁决落地，2026-09-18）。

背景
----
TD5 全库审计实证：gpfx 库 127/127 张表字符集为 ``utf8_general_ci``（utf8mb3，
每字符最多 3 字节），库默认 utf8，而连接串是 charset=utf8mb4。4 字节字符
（emoji、CJK 扩展 B~F 区生僻字、部分数学/金融符号）写入任何表都会被 MySQL
以 1366 ``Incorrect string value`` 拒绝——连接层的 utf8mb4 只是给了「支持
emoji」的错觉，落到表层照样失败。

C1 裁决（2026-09-18，需求方授权按建议执行）：**豁免 utf8mb4 大迁移**，
改为在应用写入层统一拦截 4 字节字符——

- 对已成功入库的历史数据零影响（utf8mb3 表里不可能存有 4 字节字符）；
- 把 DB 层的 1366 报错提前为应用层 ValueError（带 类名.字段名 与坏字符
  码点），从「莫名的插入失败」变成「指名道姓的输入校验失败」；
- 未来真有 4 字节字符需求时，先立 utf8mb4 迁移卡、再摘除本守卫。

实现
----
``install_bmp_text_guard()`` 对 SQLAlchemy ``Session`` 类挂全局
``before_flush`` 监听（模块 import 时自动执行；app/db/session.py 引入本模块）。
监听只扫描本次 flush 的 ``session.new + session.dirty`` 中**发生变更的字符串列
新值**（``history.added``），不做全量扫描；JSON/bytes/数值列不在拦截范围
（JSON 内部字符串交给下游校验，保持本守卫最小边界）。

幂等：重复调用 ``install_bmp_text_guard()`` 只挂一次，返回是否新挂。
"""
from __future__ import annotations

from typing import Any, List

# 4 字节 UTF-8 字符从 U+10000（BMP 之上）开始；utf8mb3 只能存 U+0000~U+FFFF
_4BYTE_MIN = 0x10000
_MAX_REPORT = 5          # 单个字段最多报 5 个坏字符（防超长噪音）
_MAX_FIELDS = 8          # 单次 flush 最多报 8 个字段


def find_4byte_chars(value: Any) -> str:
    """返回字符串中 4 字节字符的描述串（去重、限 5 个）；无则返回空串。

    >>> find_4byte_chars("普通中文 ok")
    ''
    >>> find_4byte_chars("a\\U0001F600b")   # 😀
    "'\\U0001F600'(U+1F600)"
    """
    if not isinstance(value, str):
        return ""
    seen: List[str] = []
    seen_set = set()
    for ch in value:
        if ord(ch) >= _4BYTE_MIN and ch not in seen_set:
            seen_set.add(ch)
            seen.append(f"'{ch}'(U+{ord(ch):04X})")
            if len(seen) >= _MAX_REPORT:
                break
    return " ".join(seen)


def assert_bmp_text(value: Any, field: str) -> None:
    """校验单个值为 BMP 内文本（utf8mb3 可存）；含 4 字节字符则抛 ValueError。"""
    bad = find_4byte_chars(value)
    if bad:
        raise ValueError(f"字段 {field} 含 4 字节字符（utf8mb3 不可存储）: {bad}")


def _before_flush_guard(session, flush_context, instances) -> None:
    """before_flush 监听：扫描本次变更的字符串列新值，命中 4 字节即抛 ValueError。"""
    from sqlalchemy import inspect as sa_inspect

    offenders: List[str] = []
    try:
        targets = list(session.new) + list(session.dirty)
    except Exception:  # pragma: no cover - session 异常态交给 SQLAlchemy 自身报错
        return
    for obj in targets:
        try:
            state = sa_inspect(obj)
        except Exception:  # noqa: BLE001 - 非 ORM 对象混入 new/dirty，跳过
            continue
        for attr in state.mapper.column_attrs:
            try:
                if attr.columns[0].type.python_type is not str:
                    continue
            except Exception:  # noqa: BLE001 - 类型不支持 python_type，跳过该列
                continue
            hist = state.attrs[attr.key].history
            if not hist.has_changes():
                continue
            for value in hist.added:
                bad = find_4byte_chars(value)
                if bad:
                    offenders.append(f"{type(obj).__name__}.{attr.key}: {bad}")
                    break
            if len(offenders) >= _MAX_FIELDS:
                break
        if len(offenders) >= _MAX_FIELDS:
            break
    if offenders:
        raise ValueError(
            "utf8mb3 写入守卫（C1 裁决 2026-09-18）：以下字段含 4 字节字符"
            "（emoji/CJK 扩展区等，全库 utf8mb3 无法存储）—— " + "; ".join(offenders)
        )


_installed = False


def install_bmp_text_guard() -> bool:
    """对 SQLAlchemy 全局 Session 类挂 before_flush 守卫（幂等）。

    返回 True=本次新挂；False=早已挂过。
    """
    global _installed
    if _installed:
        return False
    from sqlalchemy.orm import Session
    from sqlalchemy import event as sa_event

    sa_event.listens_for(Session, "before_flush")(_before_flush_guard)
    _installed = True
    return True


# 模块被 import 即生效（app/db/session.py 引入本模块 → 应用所有 session 路径覆盖）
install_bmp_text_guard()
