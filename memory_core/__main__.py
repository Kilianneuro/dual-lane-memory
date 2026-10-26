"""Command line entry point for memory_core.

    python -m memory_core init
    python -m memory_core serve
    python -m memory_core stats
    python -m memory_core pending
    python -m memory_core approve 1 --visibility origin --reason "ok"
    python -m memory_core reject 2 --reason "不要记这个"
"""

from __future__ import annotations

import argparse
import json
import sys

from .db import CONFIG_PATH, DEFAULT_CONFIG, ensure_token, load_config
from .store import MemoryError, MemoryStore


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _store() -> tuple[dict, MemoryStore]:
    cfg = load_config()
    return cfg, MemoryStore(cfg["db_path"], owner_id=cfg.get("owner_id", ""))


def cmd_init(_args: argparse.Namespace) -> int:
    if not CONFIG_PATH.is_file():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    cfg = load_config()
    ensure_token(cfg["api_token_file"])
    ensure_token(cfg["admin_token_file"])
    store = MemoryStore(cfg["db_path"], owner_id=cfg.get("owner_id", ""))
    try:
        _print({
            "ok": True,
            "db_path": cfg["db_path"],
            "api_token_file": cfg["api_token_file"],
            "admin_token_file": cfg["admin_token_file"],
            "owner_id": cfg.get("owner_id", ""),
            "stats": store.stats(),
        })
    finally:
        store.close()
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    from .server import serve
    serve(load_config())
    return 0


def cmd_stats(_args: argparse.Namespace) -> int:
    _cfg, store = _store()
    try:
        _print(store.stats())
    finally:
        store.close()
    return 0


def cmd_pending(_args: argparse.Namespace) -> int:
    _cfg, store = _store()
    try:
        _print(store.pending_approvals())
    finally:
        store.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    _cfg, store = _store()
    try:
        _print(store.decide_approval(
            args.approval_id,
            "approve",
            visibility=args.visibility,
            context_scope=args.context_scope,
            reason=args.reason,
            decided_by="owner-cli",
        ))
    finally:
        store.close()
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    _cfg, store = _store()
    try:
        _print(store.decide_approval(
            args.approval_id,
            "reject",
            reason=args.reason,
            decided_by="owner-cli",
        ))
    finally:
        store.close()
    return 0


def cmd_lifecycle(args: argparse.Namespace) -> int:
    from .lifecycle import decay_memory
    _cfg, store = _store()
    try:
        _print(decay_memory(store, now_ms=args.now_ms, dry_run=args.dry_run))
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory_core", description="Cross-conversation memory core")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create tokens and initialize the SQLite schema")
    sub.add_parser("serve", help="start the local HTTP API")
    sub.add_parser("stats", help="show memory counts")
    sub.add_parser("pending", help="list sensitive memories waiting for owner approval")

    approve = sub.add_parser("approve", help="approve a pending sensitive memory")
    approve.add_argument("approval_id", type=int)
    approve.add_argument("--visibility", choices=("global", "origin", "private"), default=None)
    approve.add_argument("--context-scope", choices=("share_all", "share_owner_only", "share_same_platform"), default=None)
    approve.add_argument("--reason", default="")

    reject = sub.add_parser("reject", help="reject a pending sensitive memory")
    reject.add_argument("approval_id", type=int)
    reject.add_argument("--reason", default="")

    lifecycle = sub.add_parser("lifecycle", help="run one decay/compress/purge pass")
    lifecycle.add_argument("--dry-run", action="store_true")
    lifecycle.add_argument("--now-ms", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {
            "init": cmd_init,
            "serve": cmd_serve,
            "stats": cmd_stats,
            "pending": cmd_pending,
            "approve": cmd_approve,
            "reject": cmd_reject,
            "lifecycle": cmd_lifecycle,
        }[args.command](args)
    except MemoryError as exc:
        print(f"[memory_core] {exc.code}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
