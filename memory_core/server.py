"""Local HTTP API for memory_core.

Bind to 127.0.0.1 only. Two tokens:
  - X-Memory-Token        normal save/search/mention/get/stats
  - X-Memory-Admin-Token  approval queue + decisions

Sensitive facts are stored but never returned by search until approved.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .db import ensure_token, load_config
from .store import (
    ApprovalPendingError,
    ForbiddenError,
    MemoryError,
    MemoryStore,
    NotFoundError,
)

log = logging.getLogger("memory_core.http")

_APPROVAL_DECIDE_RE = re.compile(r"^/v1/memory/approvals/(\d+)/decide$")
_MEMORY_GET_RE = re.compile(r"^/v1/memory/(\d+)$")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(store: MemoryStore, api_token: str, admin_token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "memory_core/1"

        # ---------------- plumbing ----------------
        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: Any) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, code: str, message: str) -> None:
            self._send(status, {"ok": False, "error": {"code": code, "message": message}})

        def _read_json(self) -> dict[str, Any]:
            raw_len = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_len)
            except ValueError:
                raise MemoryError("Content-Length 不合法")
            if length <= 0:
                return {}
            if length > 1_000_000:
                raise MemoryError("请求体过大（上限 1MB）", "PAYLOAD_TOO_LARGE", 413)
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MemoryError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(body, dict):
                raise MemoryError("请求体必须是 JSON 对象")
            return body

        def _admin_ok(self) -> bool:
            supplied = str(self.headers.get("X-Memory-Admin-Token") or "")
            return bool(admin_token) and hmac.compare_digest(supplied, admin_token)

        def _api_ok(self) -> bool:
            if self._admin_ok():
                return True
            supplied = str(self.headers.get("X-Memory-Token") or "")
            return bool(api_token) and hmac.compare_digest(supplied, api_token)

        def _require_api(self) -> bool:
            if self._api_ok():
                return True
            self._error(401, "UNAUTHORIZED", "缺少或错误的 X-Memory-Token")
            return False

        def _require_admin(self) -> bool:
            if self._admin_ok():
                return True
            self._error(401, "UNAUTHORIZED", "此操作需要 X-Memory-Admin-Token（主人审批）")
            return False

        # ---------------- GET ----------------
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/health":
                self._send(200, {"ok": True, "service": "memory_core", "version": "1"})
                return

            if path == "/v1/memory/stats":
                if not self._require_api():
                    return
                try:
                    self._send(200, store.stats())
                except Exception as exc:  # noqa: BLE001
                    self._handle_exception(exc)
                return

            if path == "/v1/memory/approvals":
                if not self._require_admin():
                    return
                try:
                    status = (parse_qs(parsed.query).get("status") or ["pending"])[0]
                    if status != "pending":
                        self._error(400, "BAD_REQUEST", "当前仅支持 status=pending")
                        return
                    self._send(200, store.pending_approvals())
                except Exception as exc:  # noqa: BLE001
                    self._handle_exception(exc)
                return

            m = _MEMORY_GET_RE.match(path)
            if m:
                if not self._api_ok():
                    self._error(401, "UNAUTHORIZED", "缺少或错误的 X-Memory-Token")
                    return
                try:
                    memory_id = int(m.group(1))
                    admin = self._admin_ok()
                    qs = parse_qs(parsed.query)
                    platform = (qs.get("platform") or [""])[0]
                    context_type = (qs.get("context_type") or [""])[0]
                    context_id = (qs.get("context_id") or [""])[0]
                    source_kind = (qs.get("source_kind") or [""])[0]
                    source_id = (qs.get("source_id") or [""])[0]
                    source = None
                    if platform or context_type or context_id:
                        source = {
                            "platform": platform,
                            "context_type": context_type,
                            "context_id": context_id,
                        }
                    elif source_kind or source_id:
                        # Legacy GET compatibility.
                        source = {"kind": source_kind, "id": source_id}
                    if not admin and not source:
                        self._error(400, "BAD_REQUEST", "普通读取需要 platform/context_type/context_id 查询参数")
                        return
                    self._send(200, store.get(memory_id, source=source, admin=admin))
                except Exception as exc:  # noqa: BLE001
                    self._handle_exception(exc)
                return

            self._error(404, "NOT_FOUND", "unknown endpoint")

        # ---------------- POST ----------------
        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json()
            except MemoryError as exc:
                self._error(exc.status, exc.code, str(exc))
                return

            try:
                if path == "/v1/memory/save":
                    if not self._require_api():
                        return
                    self._send(200, store.save(body))
                    return

                if path == "/v1/memory/search":
                    if not self._require_api():
                        return
                    result = store.search(
                        str(body.get("query") or ""),
                        body.get("source"),
                        limit=int(body.get("limit") or 8),
                        min_weight=float(body.get("min_weight") or 0.0),
                        categories=body.get("categories"),
                        include_content=body.get("include_content") is True,
                    )
                    self._send(200, result)
                    return

                if path == "/v1/memory/mention":
                    if not self._require_api():
                        return
                    memory_id = int(body.get("memory_id") or 0)
                    result = store.mention(
                        memory_id,
                        body.get("source"),
                        message_id=body.get("message_id"),
                        context_excerpt=body.get("context_excerpt"),
                    )
                    self._send(200, result)
                    return

                m = _APPROVAL_DECIDE_RE.match(path)
                if m:
                    if not self._require_admin():
                        return
                    approval_id = int(m.group(1))
                    result = store.decide_approval(
                        approval_id,
                        str(body.get("decision") or ""),
                        visibility=body.get("visibility"),
                        context_scope=body.get("context_scope"),
                        reason=str(body.get("reason") or ""),
                        decided_by=str(body.get("decided_by") or "owner"),
                    )
                    self._send(200, result)
                    return

                self._error(404, "NOT_FOUND", "unknown endpoint")
            except Exception as exc:  # noqa: BLE001
                self._handle_exception(exc)

        def _handle_exception(self, exc: Exception) -> None:
            if isinstance(exc, MemoryError):
                self._error(exc.status, exc.code, str(exc))
                return
            if isinstance(exc, (NotFoundError, ForbiddenError, ApprovalPendingError)):
                self._error(exc.status, exc.code, str(exc))
                return
            if isinstance(exc, (ValueError, TypeError)):
                self._error(400, "BAD_REQUEST", str(exc))
                return
            if isinstance(exc, sqlite3.Error):
                log.exception("sqlite error")
                self._error(500, "DB_ERROR", "数据库操作失败")
                return
            log.exception("unhandled error")
            self._error(500, "INTERNAL", "内部错误")

    return Handler


def create_server(store: MemoryStore, host: str, port: int,
                  api_token: str, admin_token: str) -> ThreadingHTTPServer:
    handler = make_handler(store, api_token, admin_token)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(config: dict[str, Any] | None = None) -> None:
    cfg = config or load_config()
    api_token = ensure_token(cfg["api_token_file"])
    admin_token = ensure_token(cfg["admin_token_file"])
    store = MemoryStore(cfg["db_path"], owner_id=cfg.get("owner_id", ""))
    server = create_server(store, cfg["host"], cfg["port"], api_token, admin_token)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("memory_core listening on http://%s:%s", cfg["host"], cfg["port"])
    log.info("API token file: %s", cfg["api_token_file"])
    log.info("Admin token file: %s", cfg["admin_token_file"])
    log.info("SQLite: %s", cfg["db_path"])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping memory_core")
    finally:
        server.server_close()
        store.close()
