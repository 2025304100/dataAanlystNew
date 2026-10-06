"""为 CI 写 config/db_config.json，让黑盒/E2E 闸门与生产用同一种数据库方言。

为什么必须写这个文件、而不是设 DATABASE_URL：
- `app.core.config.load_db_config()` **只读 config/db_config.json**，完全不看环境变量；
- 且 `app/db/session.py` 的模块级引擎缓存不随 DATABASE_URL 热切换（该文件注释里就写着）。
所以"让 CI 连 MySQL"唯一可靠的入口就是这个文件。CI 上原本没有它 → 应用静默退回
SQLite，于是闸门测的是与生产不同的方言（实测就出现过"本地绿、CI 红"的
`PUT → 409 DB_INTEGRITY_VIOLATION`，而 SQLite/MySQL 约束行为差异正是这类问题的温床）。

安全：只有 CI（环境变量 CI=true）才允许写；本地执行会直接报错退出，
免得把开发者自己的 db_config.json（含真实库凭据）覆盖掉。
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "config" / "db_config.json"


def main() -> int:
    if os.environ.get("CI", "").lower() not in {"true", "1"}:
        print(
            "ci_write_db_config 只允许在 CI 里运行（需要 CI=true）。\n"
            "本地 config/db_config.json 是开发者自己的数据库配置，不会被本脚本覆盖。",
            file=sys.stderr,
        )
        return 2

    required = ("CI_DB_HOST", "CI_DB_NAME", "CI_DB_USER", "CI_DB_PASSWORD")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"缺少环境变量：{missing}", file=sys.stderr)
        return 2

    config = {
        "use_mysql": True,
        "mysql": {
            "host": os.environ["CI_DB_HOST"],
            "port": int(os.environ.get("CI_DB_PORT", "3306")),
            "database": os.environ["CI_DB_NAME"],
            "user": os.environ["CI_DB_USER"],
            "password": os.environ["CI_DB_PASSWORD"],
        },
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(TARGET, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # 权限尽力而为，不影响 CI（Windows 上也常不生效）
    # 只打印非敏感信息，避免 CI 日志里出现口令
    print(f"已写入 {TARGET.relative_to(REPO)} → MySQL {config['mysql']['host']}:"
          f"{config['mysql']['port']}/{config['mysql']['database']} (user={config['mysql']['user']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
