#!/usr/bin/env python3
"""Set a livetest instance's capture inactivity timeout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set capture.inactivity_timeout_minutes in an instance config.json."
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("INSTANCE") or os.environ.get("QUAID_INSTANCE"),
        help="Quaid instance ID; defaults to INSTANCE or QUAID_INSTANCE.",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        required=True,
        help="Timeout in minutes.",
    )
    parser.add_argument(
        "--quaid-home",
        type=Path,
        default=Path(os.environ.get("QUAID_HOME", "~/.quaid")).expanduser(),
        help="Quaid home; defaults to QUAID_HOME or ~/.quaid.",
    )
    args = parser.parse_args()

    instance = str(args.instance or "").strip()
    if not instance:
        parser.error("--instance or INSTANCE/QUAID_INSTANCE is required")
    if args.minutes < 1:
        parser.error("--minutes must be >= 1")

    config_path = args.quaid_home / "instances" / instance / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{config_path} must contain a JSON object")
    data.pop("inactivity_timeout_minutes", None)
    capture = data.setdefault("capture", {})
    if not isinstance(capture, dict):
        raise RuntimeError(f"{config_path} capture must be a JSON object")
    capture["inactivity_timeout_minutes"] = args.minutes
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"set {config_path}: capture.inactivity_timeout_minutes={args.minutes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
