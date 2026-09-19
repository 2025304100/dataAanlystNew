# -*- coding: utf-8 -*-
"""TD6 守卫测试：utf8mb3 写入守卫（app/core/text_charset.py）。

覆盖：
- find_4byte_chars：BMP 文本放行 / emoji、CJK 扩展 B 检出（含码点）/ 非 str 安全
- assert_bmp_text：命中抛 ValueError 带字段名
- install_bmp_text_guard：幂等
- before_flush 端到端（内存 SQLite）：合法中文 flush 通过；4 字节 flush 抛
  ValueError 且消息带 类名.字段名；dirty 未变更列不误伤
"""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.text_charset import (
    assert_bmp_text,
    find_4byte_chars,
    install_bmp_text_guard,
)


class TestFind4ByteChars:
    def test_bmp_text_passes(self):
        assert find_4byte_chars("") == ""
        assert find_4byte_chars("普通中文 ok 123 ~!@#") == ""
        # U+FFFF 恰好是 BMP 上界（utf8mb3 可存）
        assert find_4byte_chars("\uFFFF") == ""

    def test_detects_emoji_with_codepoint(self):
        out = find_4byte_chars("a\U0001F600b")  # 😀 U+1F600
        assert "U+1F600" in out and "\U0001F600" in out

    def test_detects_cjk_ext_b(self):
        out = find_4byte_chars("\u5409\u5409\U00020BB7")  # 𠮷 U+20BB7
        assert "U+20BB7" in out

    def test_dedup_and_cap(self):
        # 6 个不同坏字符 → 去重 6 个、上限截断为 5（第 6 个不出现）
        chars = ["\U0001F600", "\U0001F601", "\U0001F602", "\U0001F603",
                 "\U0001F604", "\U00020BB7"]
        out = find_4byte_chars("x".join(chars))
        assert out.count("(U+") == 5
        assert "U+1F600" in out and "U+1F604" in out
        assert "U+20BB7" not in out

    def test_non_str_safe(self):
        assert find_4byte_chars(None) == ""
        assert find_4byte_chars(123) == ""
        assert find_4byte_chars(b"bytes") == ""


class TestAssertBmpText:
    def test_raises_with_field_name(self):
        with pytest.raises(ValueError, match="note") as excinfo:
            assert_bmp_text("x\U0001F600", "note")
        assert "U+1F600" in str(excinfo.value)

    def test_bmp_ok(self):
        assert_bmp_text("组合因子快照 v1", "note")  # 不抛


class TestInstallIdempotent:
    def test_install_twice(self):
        # 模块 import 时已挂过一次
        assert install_bmp_text_guard() is False


_Base = declarative_base()


class _GuardProbe(_Base):
    """独立小表（不碰 app.models），验证 before_flush 全链。"""

    __tablename__ = "guard_probe_td6"
    id = Column(Integer, primary_key=True)
    note = Column(Text, nullable=True)


class TestBeforeFlushGuard:
    @staticmethod
    def _make_session():
        eng = create_engine("sqlite://")
        _Base.metadata.create_all(eng)
        return sessionmaker(bind=eng)()

    def test_bmp_flush_ok(self):
        s = self._make_session()
        s.add(_GuardProbe(note="组合因子 ok"))
        s.flush()  # 不抛
        s.rollback()
        s.close()

    def test_emoji_flush_rejected_with_field(self):
        s = self._make_session()
        s.add(_GuardProbe(note="bad \U0001F600"))
        with pytest.raises(ValueError, match="GuardProbe.note") as excinfo:
            s.flush()
        assert "U+1F600" in str(excinfo.value)
        s.rollback()
        s.close()

    def test_unchanged_dirty_column_not_scanned(self):
        s = self._make_session()
        p = _GuardProbe(id=1, note="ok")
        s.add(p)
        s.flush()
        # dirty 但该列无变更（touch 同值）→ 不误伤
        p.note = "ok"
        s.flush()  # 不抛
        s.rollback()
        s.close()

    def test_emoji_via_update_dirty(self):
        s = self._make_session()
        p = _GuardProbe(id=1, note="ok")
        s.add(p)
        s.flush()
        p.note = "\U0001F601 upd"  # U+1F601
        with pytest.raises(ValueError, match="GuardProbe.note"):
            s.flush()
        s.rollback()
        s.close()
