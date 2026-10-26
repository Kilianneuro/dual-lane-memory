"""SQLite connection, config, and local token helpers for memory_core."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SCHEMA_PATH = BASE_DIR / "schema.sql"

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 9881,
    "db_path": "memory.db",
    "owner_id": "",
    "api_token_file": "api-token",
    "admin_token_file": "admin-token",
}


def _resolve_path(value: str) -> str:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"读取 {CONFIG_PATH} 失败：{exc}") from exc

    cfg["host"] = str(cfg.get("host") or DEFAULT_CONFIG["host"])
    cfg["port"] = int(cfg.get("port") or DEFAULT_CONFIG["port"])
    cfg["owner_id"] = str(cfg.get("owner_id") or "").strip()
    cfg["db_path"] = _resolve_path(str(cfg.get("db_path") or DEFAULT_CONFIG["db_path"]))
    cfg["api_token_file"] = _resolve_path(str(cfg.get("api_token_file") or DEFAULT_CONFIG["api_token_file"]))
    cfg["admin_token_file"] = _resolve_path(str(cfg.get("admin_token_file") or DEFAULT_CONFIG["admin_token_file"]))
    return cfg


def read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def ensure_token(path: str) -> str:
    existing = read_token(path)
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return token


class MemoryDB:
    """One SQLite connection shared by the store, guarded by MemoryStore's lock."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            schema = SCHEMA_PATH.read_text(encoding="utf-8")
            self.conn.executescript(schema)
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
