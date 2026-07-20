#!/usr/bin/env python3
"""Remove the M7 system-context canary from visible files and memory DB."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterable

IDENTITY_FILES = ("SOUL.md", "USER.md", "ENVIRONMENT.md")
DEFAULT_MARKER = "Bartholomew"


def _pending_signal_files(data_dir: Path) -> list[Path]:
    signal_dir = data_dir / "extraction-signals"
    if not signal_dir.exists():
        return []
    return sorted(path for path in signal_dir.iterdir() if path.is_file())


def _candidate_files(instance_root: Path) -> list[Path]:
    files = [instance_root / name for name in IDENTITY_FILES]
    files.extend(sorted(instance_root.glob("*.snippets.md")))
    return files


def _strip_marker_lines(path: Path, marker: str) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if marker not in line]
    removed = len(lines) - len(kept)
    if removed:
        path.write_text("".join(kept), encoding="utf-8")
    return removed


def _like_contains_pattern(value: str) -> str:
    escaped = (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _memory_node_ids(db_path: Path, marker: str) -> list[str]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE name LIKE ? ESCAPE '\\' ORDER BY created_at, id",
            (_like_contains_pattern(marker),),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _resolve_quaid_cli(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env_cli = os.environ.get("QCLI")
    if env_cli:
        return env_cli
    path_cli = shutil.which("quaid")
    if path_cli:
        return path_cli
    bundled = Path.home() / ".quaid" / "plugins" / "quaid" / "quaid"
    if bundled.exists():
        return str(bundled)
    return None


def _delete_nodes(node_ids: Iterable[str], *, instance: str, quaid_home: Path, quaid_cli: str) -> None:
    env = {
        **os.environ,
        "QUAID_HOME": str(quaid_home),
        "QUAID_INSTANCE": instance,
    }
    for node_id in node_ids:
        result = subprocess.run(
            [quaid_cli, "delete", node_id],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"quaid delete failed for {node_id}: stdout={result.stdout!r} stderr={result.stderr!r}"
            )


def _files_with_marker(paths: Iterable[Path], marker: str) -> list[str]:
    hits: list[str] = []
    for path in paths:
        try:
            if marker in path.read_text(encoding="utf-8"):
                hits.append(str(path))
        except FileNotFoundError:
            continue
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean the M7 Bartholomew canary from visible-home snippets and memory DB."
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("INSTANCE") or os.environ.get("QUAID_INSTANCE"),
        help="Quaid instance ID; defaults to INSTANCE or QUAID_INSTANCE.",
    )
    parser.add_argument(
        "--marker",
        default=DEFAULT_MARKER,
        help="Unique canary marker to remove and verify.",
    )
    parser.add_argument(
        "--visible-home",
        type=Path,
        default=Path.home() / "quaid",
        help="Visible Quaid home; defaults to ~/quaid.",
    )
    parser.add_argument(
        "--quaid-home",
        type=Path,
        default=Path.home() / ".quaid",
        help="Hidden Quaid home; defaults to ~/.quaid.",
    )
    parser.add_argument(
        "--quaid-cli",
        default=None,
        help="Path to the installed quaid CLI; defaults to QCLI, PATH, then ~/.quaid/plugins/quaid/quaid.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify that no marker residue remains; do not mutate files or memory.",
    )
    args = parser.parse_args()

    instance = str(args.instance or "").strip()
    marker = str(args.marker or "").strip()
    if not instance:
        print("FAIL: --instance or INSTANCE/QUAID_INSTANCE is required", file=sys.stderr)
        return 1
    if not marker:
        print("FAIL: --marker must be non-empty", file=sys.stderr)
        return 1

    visible_instance_root = args.visible_home.expanduser() / "instances" / instance
    quaid_home = args.quaid_home.expanduser()
    data_dir = quaid_home / "instances" / instance / "data"
    if not data_dir.is_dir():
        print(f"FAIL: instance data directory not found: {data_dir}", file=sys.stderr)
        return 1
    if not os.access(data_dir, os.R_OK | os.X_OK):
        print(f"FAIL: instance data directory is not readable/searchable: {data_dir}", file=sys.stderr)
        return 1
    try:
        pending_signals = _pending_signal_files(data_dir)
    except OSError as exc:
        print(f"FAIL: could not inspect pending extraction signals under {data_dir}: {exc}", file=sys.stderr)
        return 1
    if pending_signals:
        print("FAIL: pending extraction signals remain; drain daemon before M7 cleanup:", file=sys.stderr)
        for path in pending_signals[:20]:
            print(f"  {path}", file=sys.stderr)
        if len(pending_signals) > 20:
            print(f"  ... {len(pending_signals) - 20} more", file=sys.stderr)
        return 1

    db_path = data_dir / "memory.db"
    files = _candidate_files(visible_instance_root)

    node_ids = _memory_node_ids(db_path, marker)
    removed_lines = 0
    if not args.verify_only:
        removed_lines = sum(_strip_marker_lines(path, marker) for path in files)

    if node_ids and not args.verify_only:
        quaid_cli = _resolve_quaid_cli(args.quaid_cli)
        if quaid_cli is None:
            print(
                "FAIL: canary memory nodes exist but no quaid CLI was found for deletion",
                file=sys.stderr,
            )
            for node_id in node_ids:
                print(node_id, file=sys.stderr)
            return 1
        try:
            _delete_nodes(node_ids, instance=instance, quaid_home=quaid_home, quaid_cli=quaid_cli)
        except Exception as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

    remaining_files = _files_with_marker(files, marker)
    remaining_nodes = _memory_node_ids(db_path, marker)
    if remaining_files or remaining_nodes:
        if remaining_files:
            print("FAIL: canary marker remains in visible-home files:", file=sys.stderr)
            for path in remaining_files:
                print(f"  {path}", file=sys.stderr)
        if remaining_nodes:
            print("FAIL: canary marker remains in memory nodes:", file=sys.stderr)
            for node_id in remaining_nodes:
                print(f"  {node_id}", file=sys.stderr)
        return 1

    if args.verify_only:
        print(f"M7 canary cleanup verification clean: instance={instance} marker={marker!r}")
    else:
        print(
            f"M7 canary cleanup complete: instance={instance} marker={marker!r} "
            f"removed_lines={removed_lines} deleted_nodes={len(node_ids)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
