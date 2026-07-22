"""统一 Secret Store（WP-AI.2 多 Profile 主备降级）。

为 AI Profile 与消息渠道提供统一的 Secret 存储抽象。

设计要点：
- 优先级：环境变量 > 系统凭据 > 主密钥加密文件
- 永不返回明文 Secret 到日志（log.debug 只记录 key 名）
- __repr__ / __str__ 不暴露值
- AI 与消息渠道共用同一 Secret Store

第一阶段实现：
- 环境变量后端：os.environ.get(key)
- 加密文件后端：secrets.enc（master key 加密），加密采用 base64 占位
  （与 app/utils/secret_mask.py 同策略，后续可替换为 AES-256-GCM）
- 系统凭据后端：预留接口（Windows Credential Manager / macOS Keychain /
  Linux secret-service），第一阶段未实现，返回 None
- master key 从环境变量 APP_MASTER_KEY 读取，若不存在生成随机值
  （仅当前进程有效，重启后加密文件后端的 secret 不可解密）

project_memory 硬约束：
- 永不返回明文 Secret 到日志
- API 响应、导出功能不包含 secret 值
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets as _pysecrets
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 默认加密文件路径（在项目根目录 config/secrets.enc）
_DEFAULT_SECRETS_PATH = Path(
    os.getenv(
        "APP_SECRETS_PATH",
        str(Path(__file__).resolve().parents[2] / "config" / "secrets.enc"),
    )
).expanduser()


class SecretStore:
    """统一 Secret 存储（WP-AI.2）。

    优先级：环境变量 > 系统凭据 > 主密钥加密文件

    所有方法只 log.debug 记录 key 名，不记录值。
    """

    def __init__(
        self,
        *,
        secrets_path: Path | str | None = None,
        master_key: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._secrets_path = Path(secrets_path) if secrets_path else _DEFAULT_SECRETS_PATH
        self._master_key = master_key or self._load_or_generate_master_key()
        # 内存缓存（加密文件后端的解密结果）
        self._cache: dict[str, str] = {}
        self._cache_loaded = False

    # ── 公共 API ──────────────────────────────────────────────

    def get_secret(self, key: str) -> str | None:
        """获取 Secret，永不返回明文到日志。

        优先级：环境变量 > 系统凭据 > 加密文件
        """
        if not key or not isinstance(key, str):
            return None
        # 1. 环境变量优先
        value = os.environ.get(key)
        if value is not None:
            logger.debug("SecretStore: hit env var for key=%s", key)
            return value
        # 2. 系统凭据（第一阶段未实现）
        value = self._get_from_system_credential(key)
        if value is not None:
            logger.debug("SecretStore: hit system credential for key=%s", key)
            return value
        # 3. 加密文件后端
        value = self._get_from_encrypted_file(key)
        if value is not None:
            logger.debug("SecretStore: hit encrypted file for key=%s", key)
            return value
        logger.debug("SecretStore: miss for key=%s", key)
        return None

    def set_secret(self, key: str, value: str) -> None:
        """设置 Secret（写入加密文件后端）。

        注意：环境变量后端只读，不可通过此方法设置。
        """
        if not key or not isinstance(key, str):
            raise ValueError("key must be a non-empty string")
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        with self._lock:
            self._load_encrypted_file(force=True)
            self._cache[key] = value
            self._flush_encrypted_file()
            logger.debug("SecretStore: set secret for key=%s", key)

    def delete_secret(self, key: str) -> bool:
        """删除 Secret（仅从加密文件后端删除）。"""
        if not key:
            return False
        with self._lock:
            self._load_encrypted_file(force=True)
            if key not in self._cache:
                return False
            del self._cache[key]
            self._flush_encrypted_file()
            logger.debug("SecretStore: deleted secret for key=%s", key)
            return True

    def list_keys(self) -> list[str]:
        """列出所有 key（不返回值）。

        合并环境变量、系统凭据（第一阶段无）、加密文件中的 key。
        """
        keys: set[str] = set()
        # 环境变量中的 key（无法区分哪些是 secret，因此只返回加密文件中的）
        # 加密文件中的 key
        with self._lock:
            self._load_encrypted_file(force=False)
            keys.update(self._cache.keys())
        return sorted(keys)

    # ── 内部方法 ──────────────────────────────────────────────

    def _load_or_generate_master_key(self) -> str:
        """从环境变量 APP_MASTER_KEY 读取，若不存在生成随机值。"""
        env_key = os.environ.get("APP_MASTER_KEY")
        if env_key:
            return env_key
        # 生成随机值（仅当前进程有效）
        return _pysecrets.token_urlsafe(32)

    def _get_from_system_credential(self, key: str) -> str | None:
        """从系统凭据存储获取（第一阶段未实现）。"""
        return None

    def _load_encrypted_file(self, *, force: bool = False) -> None:
        """加载加密文件到内存缓存。"""
        if self._cache_loaded and not force:
            return
        self._cache = {}
        if not self._secrets_path.exists():
            self._cache_loaded = True
            return
        try:
            raw = self._secrets_path.read_bytes()
            if not raw:
                self._cache_loaded = True
                return
            decrypted = self._decrypt(raw.decode("ascii"))
            if decrypted:
                data = json.loads(decrypted)
                if isinstance(data, dict):
                    # 只接受 str 值
                    self._cache = {
                        str(k): str(v) for k, v in data.items() if v is not None
                    }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("SecretStore: failed to load encrypted file: %s", exc)
        finally:
            self._cache_loaded = True

    def _flush_encrypted_file(self) -> None:
        """将内存缓存写入加密文件。"""
        try:
            self._secrets_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._cache, ensure_ascii=False)
            encrypted = self._encrypt(payload)
            # 原子写：先写临时文件再 rename
            tmp_path = self._secrets_path.with_suffix(".tmp")
            tmp_path.write_bytes(encrypted.encode("ascii"))
            tmp_path.replace(self._secrets_path)
            # 限制权限（Windows 上可能不生效）
            try:
                os.chmod(str(self._secrets_path), 0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.warning("SecretStore: failed to flush encrypted file: %s", exc)

    def _get_from_encrypted_file(self, key: str) -> str | None:
        """从加密文件后端获取 Secret。"""
        with self._lock:
            self._load_encrypted_file(force=False)
            return self._cache.get(key)

    def _encrypt(self, plaintext: str) -> str:
        """加密（第一阶段：base64 占位，可替换为 AES-256-GCM）。"""
        if not plaintext:
            return ""
        # 简单混淆：base64(master_key + payload)
        # 注意：这不是真正的加密，第一阶段占位实现
        combined = f"{self._master_key}:{plaintext}"
        return base64.b64encode(combined.encode("utf-8")).decode("ascii")

    def _decrypt(self, ciphertext: str) -> str:
        """解密（与 _encrypt 配套）。"""
        if not ciphertext:
            return ""
        try:
            combined = base64.b64decode(ciphertext.encode("ascii")).decode("utf-8")
            # 校验 master_key 前缀
            prefix = f"{self._master_key}:"
            if not combined.startswith(prefix):
                logger.warning("SecretStore: master key mismatch on decrypt")
                return ""
            return combined[len(prefix):]
        except Exception as exc:
            logger.warning("SecretStore: decrypt failed: %s", exc)
            return ""

    def __repr__(self) -> str:
        return f"<SecretStore(path={self._secrets_path}, keys={len(self.list_keys())})>"

    def __str__(self) -> str:
        return self.__repr__()


# ── 单例 ────────────────────────────────────────────────────

_singleton: SecretStore | None = None
_singleton_lock = threading.Lock()


def get_secret_store() -> SecretStore:
    """获取全局 SecretStore 单例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SecretStore()
    return _singleton


def reset_secret_store() -> None:
    """重置单例（仅用于测试）。"""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "SecretStore",
    "get_secret_store",
    "reset_secret_store",
]
