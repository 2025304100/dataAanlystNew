from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote_plus


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_choice(name: str, *, choices: set[str], default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in choices else default


class Settings:
    def __init__(self) -> None:
        base_dir = Path(__file__).resolve().parents[2]
        default_db_path = Path(tempfile.gettempdir()) / "quant_workbench.db"
        default_factor_warehouse_path = base_dir / "tmp" / "factor_warehouse.duckdb"
        self.app_name = "Personal Quant Workbench API"
        self.api_prefix = "/api/v1"
        self.base_dir = base_dir
        self.database_url = os.getenv(
            "DATABASE_URL",
            f"sqlite:///{default_db_path.as_posix()}",
        )
        self.factor_feature_enabled = _env_bool("FACTOR_FEATURE_ENABLED", False)
        self.factor_weight_mode = _env_choice(
            "FACTOR_WEIGHT_MODE",
            choices={"manual", "shadow", "ridge"},
            default="manual",
        )
        self.factor_warehouse_path = Path(
            os.getenv("FACTOR_WAREHOUSE_PATH", str(default_factor_warehouse_path))
        ).expanduser()
        self.wxpusher_enabled = _env_bool("WXPUSHER_ENABLED", False)
        self.wxpusher_endpoint = os.getenv(
            "WXPUSHER_ENDPOINT",
            "https://wxpusher.zjiecode.com/api/send/message",
        )
        self.wxpusher_app_token = os.getenv("WXPUSHER_APP_TOKEN", "")
        self.wxpusher_uid = os.getenv("WXPUSHER_UID", "")
        # WP0：机会中心改造功能开关（默认关闭，开发环境可通过环境变量开启）
        # 注意：这 4 个字段使用大写属性名以匹配任务规范，便于在业务代码中作为显式开关标识
        self.OPPORTUNITY_CENTER_ENABLED = _env_bool("OPPORTUNITY_CENTER_ENABLED", False)
        self.PORTFOLIO_MEMBERS_ENABLED = _env_bool("PORTFOLIO_MEMBERS_ENABLED", False)
        # WP9.5：成员来源开关默认值改为 True，停止读取最新扫描作为默认来源。
        # 旧默认值为 False（使用持仓+最新扫描），WP6/WP7 双轨验收通过后切换为 True。
        # 如需回退到旧行为，显式设置环境变量 AUTO_TRADE_MEMBER_SOURCE_ENABLED=false。
        self.AUTO_TRADE_MEMBER_SOURCE_ENABLED = _env_bool("AUTO_TRADE_MEMBER_SOURCE_ENABLED", True)
        self.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = _env_bool(
            "PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED", True
        )
        # WP-S：外部数据网关稳定性底座配置
        # 单飞/限流/熔断/缓存的运行时参数，可通过环境变量覆盖
        # 主机级并发上限（同一 host 同时允许的活跃请求）
        self.EXTERNAL_DATA_HOST_CONCURRENCY = int(
            os.getenv("EXTERNAL_DATA_HOST_CONCURRENCY", "5")
        )
        # 接口级并发上限（同一 interface_key 同时允许的活跃请求）
        self.EXTERNAL_DATA_INTERFACE_CONCURRENCY = int(
            os.getenv("EXTERNAL_DATA_INTERFACE_CONCURRENCY", "2")
        )
        # 任务类型级并发上限（同一 task_type 同时允许的活跃请求）
        self.EXTERNAL_DATA_TASK_TYPE_CONCURRENCY = int(
            os.getenv("EXTERNAL_DATA_TASK_TYPE_CONCURRENCY", "10")
        )
        # L1 进程缓存默认 TTL（秒）：行情类 60s，可在网关内部按 interface_key 细分
        self.EXTERNAL_DATA_L1_CACHE_TTL_SECONDS = int(
            os.getenv("EXTERNAL_DATA_L1_CACHE_TTL_SECONDS", "60")
        )
        # L1 缓存最大条目数（防止内存膨胀）
        self.EXTERNAL_DATA_L1_CACHE_MAXSIZE = int(
            os.getenv("EXTERNAL_DATA_L1_CACHE_MAXSIZE", "512")
        )
        # 熔断阈值：连续失败 N 次后进入 open 状态
        self.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD = int(
            os.getenv("EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD", "5")
        )
        # 熔断初始冷却时间（秒）：open → half_open 的初始间隔
        self.EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS = int(
            os.getenv("EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS", "60")
        )
        # 熔断最大冷却时间（秒）：探测失败后 cooldown 翻倍上限（默认 30 分钟）
        self.EXTERNAL_DATA_BREAKER_MAX_COOLDOWN_SECONDS = int(
            os.getenv("EXTERNAL_DATA_BREAKER_MAX_COOLDOWN_SECONDS", "1800")
        )
        # 指数退避：base/max/jitter（秒）
        self.EXTERNAL_DATA_BACKOFF_BASE_SECONDS = float(
            os.getenv("EXTERNAL_DATA_BACKOFF_BASE_SECONDS", "1.0")
        )
        self.EXTERNAL_DATA_BACKOFF_MAX_SECONDS = float(
            os.getenv("EXTERNAL_DATA_BACKOFF_MAX_SECONDS", "60.0")
        )
        self.EXTERNAL_DATA_BACKOFF_JITTER = float(
            os.getenv("EXTERNAL_DATA_BACKOFF_JITTER", "0.3")
        )
        # 批量入库默认分块大小（行/块）
        self.EXTERNAL_DATA_UPSERT_BATCH_SIZE = int(
            os.getenv("EXTERNAL_DATA_UPSERT_BATCH_SIZE", "500")
        )
        # 批量入库失败块最大重试次数
        self.EXTERNAL_DATA_UPSERT_MAX_RETRIES = int(
            os.getenv("EXTERNAL_DATA_UPSERT_MAX_RETRIES", "3")
        )
        # 网关功能开关（默认开启，可通过环境变量关闭以回退到旧直接调用模式）
        self.EXTERNAL_DATA_GATEWAY_ENABLED = _env_bool(
            "EXTERNAL_DATA_GATEWAY_ENABLED", True
        )
        # WP-AI.6：结构化输出与审计配置
        # 会话保留天数（超过后由 cleanup_expired_sessions 归档）
        self.AI_SESSION_RETENTION_DAYS = int(
            os.getenv("AI_SESSION_RETENTION_DAYS", "90")
        )
        # 是否启用审计（关闭后 create_audit_record 直接返回 None）
        self.AI_AUDIT_ENABLED = _env_bool("AI_AUDIT_ENABLED", True)
        # 审计上下文最大大小（字符），超出截断
        self.AI_AUDIT_MAX_CONTEXT_SIZE = int(
            os.getenv("AI_AUDIT_MAX_CONTEXT_SIZE", "2048")
        )


settings = Settings()

# ── 数据库配置文件管理 ─────────────────────────────────────

DB_CONFIG_PATH = settings.base_dir / "config" / "db_config.json"

_DEFAULT_DB_CONFIG: dict = {
    "use_mysql": False,
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "database": "",
        "user": "",
        "password": "",
    },
}


def load_db_config() -> dict:
    """从 config/db_config.json 读取数据库配置，不存在则返回默认值。"""
    if not DB_CONFIG_PATH.exists():
        return json.loads(json.dumps(_DEFAULT_DB_CONFIG))  # deep copy
    try:
        data = json.loads(DB_CONFIG_PATH.read_text(encoding="utf-8"))
        # 确保结构完整
        for key in _DEFAULT_DB_CONFIG:
            if key not in data:
                data[key] = _DEFAULT_DB_CONFIG[key]
        if isinstance(data.get("mysql"), dict):
            for mk, mv in _DEFAULT_DB_CONFIG["mysql"].items():
                data["mysql"].setdefault(mk, mv)
        return data
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_DEFAULT_DB_CONFIG))


def save_db_config(config: dict) -> None:
    """将数据库配置写入 config/db_config.json，并限制文件权限。"""
    DB_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # 尝试限制文件权限（Windows 上可能不生效，但不影响功能）
    try:
        os.chmod(str(DB_CONFIG_PATH), stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def build_mysql_url(cfg: dict) -> str:
    """根据 MySQL 配置构建 SQLAlchemy 连接 URL。

    密码中的特殊字符通过 urllib.parse.quote_plus 编码。
    """
    m = cfg.get("mysql", cfg)
    host = m.get("host", "127.0.0.1")
    port = m.get("port", 3306)
    database = m.get("database", "")
    user = m.get("user", "")
    password = m.get("password", "")
    encoded_pw = quote_plus(password) if password else ""
    encoded_user = quote_plus(user) if user else ""
    return f"mysql+pymysql://{encoded_user}:{encoded_pw}@{host}:{port}/{database}?charset=utf8mb4"
