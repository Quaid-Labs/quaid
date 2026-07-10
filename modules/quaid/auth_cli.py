"""Quaid auth helper commands."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _read_token_file(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Failed reading token file {path}: {exc}") from exc
    if not token:
        raise RuntimeError(f"Token file is empty: {path}")
    return token


def _resolve_refresh_token(args: argparse.Namespace) -> str:
    token = str(getattr(args, "token", "") or "").strip()
    if token:
        return token

    raw_file = str(getattr(args, "file", "") or "").strip()
    env_file = (
        os.environ.get("QUAID_AUTH_TOKEN_FILE", "").strip()
        or os.environ.get("CC_AUTH_TOKEN_FILE", "").strip()
    )
    token_file = raw_file or env_file
    if token_file:
        return _read_token_file(Path(token_file).expanduser())

    stdin_token = sys.stdin.read().strip()
    if stdin_token:
        return stdin_token

    raise RuntimeError(
        "No token provided. Usage: quaid auth refresh <token> or "
        "quaid auth refresh --file /path/to/anthtoken.md"
    )

def _resolve_refresh_kind(adapter, args: argparse.Namespace, token: str) -> str:
    explicit = str(getattr(args, "kind", "") or "").strip().lower()
    if explicit:
        return explicit
    inferred = adapter.infer_auth_token_kind(token)
    if inferred:
        return inferred
    raise RuntimeError(
        "Could not infer credential kind. Pass --kind "
        "(anthropic_oauth|anthropic_api|codex_oauth|openai_api)."
    )


def cmd_refresh(args: argparse.Namespace) -> int:
    from lib.adapter import get_adapter

    adapter = get_adapter()
    token = _resolve_refresh_token(args)
    kind = _resolve_refresh_kind(adapter, args, token)
    stored_path = adapter.store_shared_auth_token(kind, token)
    print(f"Auth credential ({kind}) stored at {stored_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quaid auth helper")
    sub = parser.add_subparsers(dest="cmd")

    refresh_p = sub.add_parser("refresh", help="Refresh the adapter auth token")
    refresh_p.add_argument("token", nargs="?", help="Token value (omit to read from stdin or --file)")
    refresh_p.add_argument("--file", help="Read token from a file path")
    refresh_p.add_argument(
        "--kind",
        help="Credential kind: anthropic_oauth, anthropic_api, codex_oauth, openai_api",
    )

    args = parser.parse_args(argv)

    if args.cmd == "refresh":
        try:
            return cmd_refresh(args)
        except Exception as exc:
            print(str(exc))
            return 1

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
