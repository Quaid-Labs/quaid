#!/usr/bin/env python3
"""Read recent Matrix messages from the OpenClaw test room."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip("'").strip('"')
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        pass
    return data


def _cfg() -> dict[str, str]:
    script_dir = Path(__file__).resolve().parent
    cfg = {}
    cfg.update(_load_env_file(Path.home() / "quaidcode" / "util" / "scripts" / ".matrix-config"))
    cfg.update(_load_env_file(script_dir / ".matrix-config"))
    for key in ("MATRIX_HOMESERVER", "MATRIX_TOKEN", "MATRIX_ROOM_ID", "MATRIX_READ_LIMIT"):
        value = os.environ.get(key, "").strip()
        if value:
            cfg[key] = value
    return cfg


def main() -> int:
    cfg = _cfg()
    homeserver = str(cfg.get("MATRIX_HOMESERVER") or cfg.get("HOMESERVER") or "").strip().rstrip("/")
    token = str(cfg.get("MATRIX_TOKEN") or cfg.get("TOKEN") or "").strip()
    room_id = str(cfg.get("MATRIX_ROOM_ID") or cfg.get("ROOM") or "").strip()
    try:
        limit = int(str(cfg.get("MATRIX_READ_LIMIT") or "20").strip() or "20")
    except ValueError:
        limit = 20
    if not homeserver or not token or not room_id:
        print(
            "matrix-read config missing. Set MATRIX_HOMESERVER, MATRIX_TOKEN, MATRIX_ROOM_ID "
            "via env or scripts/.matrix-config",
            file=sys.stderr,
        )
        return 2

    room_enc = urllib.parse.quote(room_id, safe="")
    url = f"{homeserver}/_matrix/client/v3/rooms/{room_enc}/messages?dir=b&limit={max(1, limit)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    for ev in reversed(data.get("chunk", [])):
        if ev.get("type") != "m.room.message":
            continue
        sender = ev.get("sender", "?")
        body = str((ev.get("content") or {}).get("body") or "").strip()
        if body:
            print(f"{sender}: {body[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
