"""Public SessionDB bridge CLI surfaces.

The session datastore is mostly internal runtime plumbing. This module exposes
narrow, user-facing inspection commands that are safe for livetest/debug flows.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from core.services.session_memory_bridge import get_session_memory_bridge


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _default_owner_id() -> str:
    from config import get_config

    return _clean(getattr(get_config().users, "default_owner", "")) or "default"


def _pair_text(pair: Dict[str, Any]) -> str:
    text = _clean(pair.get("text"))
    if text:
        return text
    parts: List[str] = []
    user = _clean(pair.get("user_text"))
    assistant = _clean(pair.get("assistant_text"))
    if user:
        parts.append(f"User: {user}")
    if assistant:
        parts.append(f"Assistant: {assistant}")
    return "\n".join(parts)


def _print_expanded_microchunk(result: Dict[str, Any]) -> None:
    micro = result.get("microchunk") if isinstance(result.get("microchunk"), dict) else {}
    pair = result.get("pair") if isinstance(result.get("pair"), dict) else {}
    window = result.get("window") if isinstance(result.get("window"), list) else []

    if micro:
        print(f"microchunk_id: {_clean(micro.get('microchunk_id'))}")
        print(f"session_id: {_clean(micro.get('session_id'))}")
        print(f"pair_id: {_clean(micro.get('pair_id'))}")
        print()
        print("microchunk:")
        print(_clean(micro.get("text")))

    if pair:
        center = _clean(pair.get("pair_id"))
        print()
        print("expanded_pair:")
        rows = [row for row in window if isinstance(row, dict)] or [pair]
        for row in rows:
            marker = "*" if _clean(row.get("pair_id")) == center else "-"
            print(f"{marker} pair_id: {_clean(row.get('pair_id'))}")
            text = _pair_text(row)
            if text:
                print(text)
    elif micro:
        print()
        print("expanded_pair: MISSING")


def _print_expanded_chunk(result: Dict[str, Any]) -> None:
    window = result.get("window") if isinstance(result.get("window"), list) else []
    center = _clean(result.get("chunk_id"))
    print(f"chunk_id: {center}")
    print(f"session_id: {_clean(result.get('session_id'))}")
    print(f"chunk_index: {_clean(result.get('chunk_index'))}")
    microchunk_id = _clean(result.get("microchunk_id"))
    pair_id = _clean(result.get("message_pair_id") or result.get("pair_id"))
    if microchunk_id:
        print(f"microchunk_id: {microchunk_id}")
    if pair_id:
        print(f"pair_id: {pair_id}")
    print()
    print("chunk:")
    print(_clean(result.get("text") or result.get("content")))
    if window:
        print()
        print("window:")
        for row in window:
            if not isinstance(row, dict):
                continue
            marker = "*" if _clean(row.get("chunk_id")) == center else "-"
            index = _clean(row.get("chunk_index"))
            print(f"{marker} chunk_id: {_clean(row.get('chunk_id'))} index: {index}")
            text = _clean(row.get("text") or row.get("content"))
            if text:
                print(text)


def cmd_expand_microchunk(args: argparse.Namespace) -> int:
    owner = _clean(args.owner) or _default_owner_id()
    result = get_session_memory_bridge().expand_microchunk(
        args.microchunk_id,
        owner_id=owner,
        before=args.before,
        after=args.after,
    )
    if not result:
        print(f"error: microchunk not found: {args.microchunk_id}", file=sys.stderr)
        return 1
    pair = result.get("pair") if isinstance(result, dict) else None
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if pair else 1
    _print_expanded_microchunk(result)
    return 0 if pair else 1


def cmd_expand_chunk(args: argparse.Namespace) -> int:
    owner = _clean(args.owner) or _default_owner_id()
    result = get_session_memory_bridge().get_session_chunk(
        args.chunk_id,
        owner_id=owner,
        before=args.before,
        after=args.after,
    )
    if not result:
        print(f"error: session chunk not found: {args.chunk_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_expanded_chunk(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quaid session inspection CLI")
    sub = parser.add_subparsers(dest="command")
    expand_chunk = sub.add_parser("expand-chunk", help="Expand a MemoryDB session chunk window")
    expand_chunk.add_argument("chunk_id", help="MemoryDB session chunk id")
    expand_chunk.add_argument("--owner", default=None, help="Owner id (defaults to config users.default_owner)")
    expand_chunk.add_argument("--before", type=int, default=0, help="Adjacent prior chunks to include")
    expand_chunk.add_argument("--after", type=int, default=0, help="Adjacent following chunks to include")
    expand_chunk.add_argument("--json", action="store_true", help="Emit JSON")
    expand = sub.add_parser("expand-microchunk", help="Expand a SessionDB microchunk to its user/assistant pair")
    expand.add_argument("microchunk_id", help="SessionDB microchunk id")
    expand.add_argument("--owner", default=None, help="Owner id (defaults to config users.default_owner)")
    expand.add_argument("--before", type=int, default=0, help="Adjacent prior pairs to include")
    expand.add_argument("--after", type=int, default=0, help="Adjacent following pairs to include")
    expand.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args == ["help"]:
        raw_args = ["--help"]
    known_commands = {"expand-chunk", "expand-microchunk"}
    help_args = {"-h", "--help"}
    if raw_args and raw_args[0] not in known_commands and raw_args[0] not in help_args:
        print(f"error: unsupported 'quaid session' command: {raw_args[0]}", file=sys.stderr)
        print("session log indexing/loading is internal runtime plumbing", file=sys.stderr)
        print(
            "Usage: quaid session expand-chunk <chunk_id> | "
            "expand-microchunk <microchunk_id> [--owner <owner_id>] "
            "[--before N] [--after N] [--json]",
            file=sys.stderr,
        )
        return 1
    parser = build_parser()
    args = parser.parse_args(raw_args)
    if args.command == "expand-chunk":
        return cmd_expand_chunk(args)
    if args.command == "expand-microchunk":
        return cmd_expand_microchunk(args)
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
