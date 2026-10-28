"""Runtime-updatable management console password.

The web password can be changed from the admin UI without restarting the
process. The new password is stored as a salted PBKDF2 hash in a separate
0600 file under the persistent data directory, so it survives restarts and
container upgrades. ``AISTUDIO_WEB_PASSWORD`` remains the bootstrap password
until a persisted password exists; after that the persisted record wins.

If the persisted password is lost, delete ``data/web_auth.json`` and restart
the service to fall back to ``AISTUDIO_WEB_PASSWORD``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("web_password")

PASSWORD_FILE_NAME = "web_auth.json"
PASSWORD_MIN_LENGTH = 8
_PBKDF2_ITERATIONS = 210_000
_VERIFY_TTL_S = 300.0
_MAX_CACHED_TOKENS = 64


@dataclass(frozen=True, slots=True)
class _PasswordRecord:
    salt: bytes
    digest: bytes
    iterations: int


_lock = threading.Lock()
_record_cache: tuple[tuple[str, int, int], _PasswordRecord] | None = None
_verified_tokens: dict[bytes, float] = {}


def web_auth_path() -> Path:
    """Return the persistent password record path."""
    override = os.getenv("AISTUDIO_WEB_AUTH_FILE")
    if override:
        return Path(override)
    return Path(settings.data_dir) / PASSWORD_FILE_NAME


def web_auth_enabled() -> bool:
    """True when env bootstrap or a persisted password enables console auth."""
    if settings.web_password and settings.web_password.strip():
        return True
    return web_auth_path().is_file()


def verify_web_password(token: str | None) -> bool:
    """Verify a console token against the persisted hash or env fallback."""
    if not token:
        return False
    token = token.strip()
    if not token:
        return False

    path = web_auth_path()
    if not path.is_file():
        env_password = (settings.web_password or "").strip()
        return bool(env_password) and hmac.compare_digest(token, env_password)

    record = _load_record(path)
    if record is None:
        # An unreadable persisted password fails closed instead of silently
        # reopening the console with the old environment password.
        log.warning("管理密码文件无法解析，拒绝控制台登录: %s", path)
        return False
    return _verify_token(token, record)


def set_web_password(new_password: str) -> None:
    """Persist a new password hash and invalidate in-memory verification cache."""
    password = new_password.strip()
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"管理密码至少需要 {PASSWORD_MIN_LENGTH} 个字符")

    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, _PBKDF2_ITERATIONS)
    payload = {
        "version": 1,
        "algorithm": "pbkdf2_sha256",
        "iterations": _PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = web_auth_path()
    _write_record(path, payload)
    with _lock:
        global _record_cache
        _record_cache = None
        _verified_tokens.clear()
    log.info("管理控制台密码已更新: %s", path)


def clear_web_password_cache() -> None:
    """Drop cached records and verified tokens (used by tests and resets)."""
    global _record_cache
    with _lock:
        _record_cache = None
        _verified_tokens.clear()


def _write_record(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}_{time.time_ns()}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    except Exception:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        raise


def _load_record(path: Path) -> _PasswordRecord | None:
    try:
        stat = path.stat()
        signature = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None

    global _record_cache
    with _lock:
        if _record_cache is not None and _record_cache[0] == signature:
            return _record_cache[1]

    record = _read_record(path)
    with _lock:
        _record_cache = (signature, record) if record is not None else None
        _verified_tokens.clear()
    return record


def _read_record(path: Path) -> _PasswordRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("读取管理密码文件失败: %s", e)
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if data.get("algorithm") != "pbkdf2_sha256":
        return None
    iterations = data.get("iterations")
    salt_value = data.get("salt")
    hash_value = data.get("hash")
    if type(iterations) is not int or not 10_000 <= iterations <= 2_000_000:
        return None
    if not isinstance(salt_value, str) or not isinstance(hash_value, str):
        return None
    try:
        salt = base64.b64decode(salt_value, validate=True)
        digest = base64.b64decode(hash_value, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(salt) < 8 or len(digest) != 32:
        return None
    return _PasswordRecord(salt=salt, digest=digest, iterations=iterations)


def _verify_token(token: str, record: _PasswordRecord) -> bool:
    key = hashlib.sha256(token.encode("utf-8")).digest()
    now = time.monotonic()
    with _lock:
        expires = _verified_tokens.get(key)
        if expires is not None and expires > now:
            return True
        if expires is not None:
            _verified_tokens.pop(key, None)

    digest = _derive(token, record.salt, record.iterations)
    if not hmac.compare_digest(digest, record.digest):
        return False

    with _lock:
        if len(_verified_tokens) >= _MAX_CACHED_TOKENS:
            _verified_tokens.clear()
        _verified_tokens[key] = now + _VERIFY_TTL_S
    return True


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
