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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quaid session inspection CLI")
    sub = parser.add_subparsers(dest="command")
    expand = sub.add_parser("expand-microchunk", help="Expand a SessionDB microchunk to its user/assistant pair")
    expand.add_argument("microchunk_id", help="SessionDB microchunk id")
    expand.add_argument("--owner", default=None, help="Owner id (defaults to config users.default_owner)")
    expand.add_argument("--before", type=int, default=0, help="Adjacent prior pairs to include")
    expand.add_argument("--after", type=int, default=0, help="Adjacent following pairs to include")
    expand.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "expand-microchunk":
        return cmd_expand_microchunk(args)
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
