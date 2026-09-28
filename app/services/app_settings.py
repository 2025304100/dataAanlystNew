"""`app_settings` 读写 + 「表 > 环境变量 > 默认值」的开关解析。

优先级约定（本模块所有布尔开关共用，PT-DEF-15 的设置升级）
==========================================================
1. **表里的值**：用户在设置页改的，最优先，改完立刻生效；
2. **环境变量**：运维兜底（例如整套环境就要关掉某能力，或 CI 里禁止真打外网）；
3. **代码默认值**：都没有时用调用方给的 default。

读路径**绝不抛错**：设置页可能还没建表（首次启动）、后台线程里可能没有可用连接、
测试环境可能压根没跑迁移。任何异常都退回下一级，并只在 warning 级别留一句，
避免"读一个开关把主链路打翻"——这与 §七.9（告警写库把调用方事务打翻）是同一类教训。

调用方手里有 Session 时**请显式传入**：读的是"这张库"的设置；不传时本模块会开一个
短会话，而进程全局 engine 在测试里是逐用例重绑的（PT-DEF-15 的守卫就是为此而加）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

# ── 已知设置键（集中声明，避免各处散写字面量）────────────────────────
DISCOVERY_AUTO_DATA_PREP_KEY = "discovery.auto_data_prep_enabled"
# 对应的运维级 env 兜底名。只在此处声明一次：之前它和登记表里的 env_var 各写一份，
# 就是“同一事实两处记录”那一类脱节隐患（体检报告 PT-DEF-13 同族）。
DISCOVERY_AUTO_DATA_PREP_ENV = "DISCOVERY_AUTO_DATA_PREP_ENABLED"

# 已知设置项登记表：后端是唯一真相。
#   - GET 返回这份表 + 每项的生效值与来源，前端只需渲染，不再自己拼 schema；
#   - PUT 只接受登记过的键，否则“往设置表里乱插键”会没人读也没人清。
APP_SETTING_REGISTRY: dict[str, dict[str, Any]] = {
    DISCOVERY_AUTO_DATA_PREP_KEY: {
        "type": "bool",
        "default": True,
        "env_var": DISCOVERY_AUTO_DATA_PREP_ENV,
        "section": "discovery",
        "label_zh": "机会扫描：数据未就绪时自动补齐",
        "description_zh": (
            "关闭后，扫描机会中心不会隐式启动数据准备任务，也不会访问第三方行情源；"
            "你仍可在机会中心手动点击“刷新数据”。"
        ),
    },
}


class SettingKeyUnknown(ValueError):
    """PUT 了一个未登记的键（路由包成 400 UNKNOWN_SETTING_KEY）。"""


class SettingValueInvalid(ValueError):
    """值与登记类型不符（路由包成 400 SETTING_VALUE_INVALID）。"""


def coerce_setting_value(key: str, raw: Any) -> Any:
    """按登记表校并归一化待写入的值。

    只存“干净”的值（布尔就存 bool），读侧才能不做一堆历史包袋转换。
    """
    spec = APP_SETTING_REGISTRY.get(key)
    if spec is None:
        raise SettingKeyUnknown(key)
    kind = spec.get("type")
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.strip().lower() in {
            "true", "false", "1", "0", "on", "off", "yes", "no",
        }:
            return raw.strip().lower() not in {"false", "0", "off", "no"}
        raise SettingValueInvalid(f"{key} 需要布尔值，实得 {raw!r}")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise SettingValueInvalid(f"{key} 需要整数，实得 {raw!r}") from None
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise SettingValueInvalid(f"{key} 需要数字，实得 {raw!r}") from None
    if kind == "str":
        if not isinstance(raw, str):
            raise SettingValueInvalid(f"{key} 需要字符串，实得 {raw!r}")
        return raw
    raise SettingValueInvalid(f"{key} 登记了未知类型 {kind!r}")


def _lookup_row(db: Session | None, key: str) -> Any | None:
    """读一行设置；表不可用（未迁移/连接异常）时返回 None，不抛。"""
    if db is None:
        return None
    try:
        return db.get(AppSetting, key)
    except Exception:
        logger.warning("app_settings lookup for %s failed; falling back", key)
        return None


def _parse_value(row: Any | None) -> Any:
    if row is None:
        return None
    try:
        return json.loads(row.value_json if row.value_json is not None else "null")
    except Exception:
        logger.warning(
            "app_settings row %s has malformed value_json=%r", getattr(row, "key", "?"),
            getattr(row, "value_json", None),
        )
        return None


def _env_value(env_var: str | None) -> Any:
    if not env_var:
        return None
    raw = os.getenv(env_var)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def list_effective_settings(db: Session | None = None) -> list[dict[str, Any]]:
    """列出全部已登记设置项的生效值与来源（供设置页渲染）。

    source 取 table / env / default：让界面能说清“这个值是你改的还是运维锁的”，
    避免“页面上改了但没效果”这类回显陷阱。
    """
    own_session = db is None
    session: Session | None = db
    if session is None:
        try:
            from app.db.session import get_session_local

            session = get_session_local()
        except Exception:
            session = None

    out: list[dict[str, Any]] = []
    try:
        for key, spec in APP_SETTING_REGISTRY.items():
            row = _lookup_row(session, key)
            table_value = _parse_value(row)
            env_value = _env_value(spec.get("env_var"))
            if table_value is not None:
                value, source = table_value, "table"
            elif env_value is not None:
                value, source = env_value, "env"
            else:
                value, source = spec.get("default"), "default"
            out.append({
                "key": key,
                "type": spec.get("type"),
                "section": spec.get("section"),
                "value": value,
                "source": source,
                "default": spec.get("default"),
                "env_var": spec.get("env_var"),
                "label_zh": spec.get("label_zh"),
                "description_zh": spec.get("description_zh"),
                "updated_by": getattr(row, "updated_by", None),
                "updated_at": getattr(row, "updated_at", None),
            })
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass
    return out


def get_app_setting(db: Session, key: str, default: Any = None) -> Any:
    """读取一个设置值（JSON 解析）。无行 / 解析失败 → default。"""
    try:
        row = db.get(AppSetting, key)
    except Exception:  # 表未建等基础设施问题不得影响主链路
        logger.warning("get_app_setting(%s) failed; falling back to default", key)
        return default
    if row is None:
        return default
    try:
        return json.loads(row.value_json if row.value_json is not None else "null")
    except Exception:
        logger.warning(
            "app_settings row %s has malformed value_json=%r; using default",
            key, row.value_json,
        )
        return default


def set_app_setting(
    db: Session,
    key: str,
    value: Any,
    *,
    actor: str = "api",
) -> AppSetting:
    """写入一个设置值（存在则覆盖），提交并返回该行。

    值统一 JSON 序列化：布尔存 ``true``、数字存字面量、字符串带引号，读侧不再猜类型。
    """
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json=json.dumps(value, default=str))
        db.add(row)
    else:
        row.value_json = json.dumps(value, default=str)
    row.updated_by = actor[:128]
    db.commit()
    db.refresh(row)
    return row


def resolve_bool_setting(
    key: str,
    *,
    default: bool = True,
    env_var: str | None = None,
    db: Session | None = None,
) -> bool:
    """按「表 > env > default」解析一个布尔开关。

    - 表里存的是真/假布尔才生效；存成 ``"0"/"1"`` 之类字符串也容忍（历史 seed 或
      手填 JSON 的常见形态）。
    - 表里没有该键（或读失败）时看 env_var；env 未设置才用 default。
    """
    own_session = db is None
    value: Any = None
    found_in_table = False
    session: Session | None = db
    try:
        if session is None:
            from app.db.session import get_session_local

            session = get_session_local()
        row = session.get(AppSetting, key)
        if row is not None:
            value = json.loads(row.value_json if row.value_json is not None else "null")
            found_in_table = value is not None
    except Exception:
        # 表不可用（未迁移/连接异常）：安静地退回 env，不影响调用方
        logger.debug("resolve_bool_setting(%s): table read failed, use env", key)
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass

    if found_in_table:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return default

    if env_var is not None:
        raw = os.getenv(env_var)
        if raw is not None and raw.strip() != "":
            return raw.strip().lower() not in {"0", "false", "no", "off"}
    return default
