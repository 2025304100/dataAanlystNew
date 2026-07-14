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
