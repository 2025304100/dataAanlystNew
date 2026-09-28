"""应用设置（`app_settings`）的对外契约。

设计要点：GET 不只返回值，还返回**类型/来源/默认值/文案**，让前端按后端登记的
元数据渲染，避免前后端各写一份"这个键叫什么、是布尔还是数字"的隐契约
（体检报告 §十 PT-DEF-13 就是"同一份枚举在代码与约束里各写一遍"那一类病）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AppSettingRead(BaseModel):
    """单个设置项（含生效值与来源）。"""

    key: str
    type: str = Field(description="登记类型：bool / int / float / str")
    section: str | None = Field(default=None, description="归属域（discovery / alerts …）")
    value: Any = Field(description="生效值（已按 表 > env > 默认 归位）")
    source: str = Field(
        default="default",
        description="值来源：table=用户在设置页改的；env=运维环境变量锁的；default=未设置",
    )
    default: Any = None
    env_var: str | None = None
    label_zh: str | None = None
    description_zh: str | None = None
    updated_by: str | None = Field(default=None, description="最后一次写入者/来源")
    updated_at: datetime | None = None


class AppSettingUpdateRequest(BaseModel):
    """写入一个设置值；值会按登记类型校验并归一化。"""

    value: Any


class AppSettingUpdateResponse(BaseModel):
    key: str
    value: Any
    source: str = "table"
    updated_by: str | None = None
