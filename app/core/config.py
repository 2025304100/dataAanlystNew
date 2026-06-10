from pathlib import Path
import os
import tempfile


class Settings:
    def __init__(self) -> None:
        base_dir = Path(__file__).resolve().parents[2]
        default_db_path = Path(tempfile.gettempdir()) / "quant_workbench.db"
        self.app_name = "Personal Quant Workbench API"
        self.api_prefix = "/api/v1"
        self.base_dir = base_dir
        self.database_url = os.getenv(
            "DATABASE_URL",
            f"sqlite:///{default_db_path.as_posix()}",
        )


settings = Settings()
