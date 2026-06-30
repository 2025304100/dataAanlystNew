"""数据库配置管理的 Pydantic 数据模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MySQLConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = ""
    user: str = ""
    password: str = ""


class DbConfigResponse(BaseModel):
    use_mysql: bool = False
    mysql: MySQLConfig = MySQLConfig()


class DbConfigUpdate(BaseModel):
    use_mysql: bool
    mysql: MySQLConfig


class TestConnectionResult(BaseModel):
    success: bool
    message: str
    server_version: str | None = None


class MigrationProgress(BaseModel):
    status: str = "idle"  # "idle" | "running" | "completed" | "failed"
    current_table: str | None = None
    tables_done: int = 0
    tables_total: int = 0
    rows_migrated: int = 0
    error: str | None = None
