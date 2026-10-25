"""Application boundaries for lossless writes to existing MySQL TEXT columns."""

MYSQL_TEXT_MAX_BYTES = 65535


def validate_mysql_text(value: str | None, *, field: str) -> str | None:
    if value is not None and len(value.encode("utf-8")) > MYSQL_TEXT_MAX_BYTES:
        raise ValueError(f"{field} UTF-8 编码不能超过 {MYSQL_TEXT_MAX_BYTES} 字节")
    return value
