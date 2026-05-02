#!/usr/bin/env python3
"""Update live-test dashboard cells and track per-cell elapsed time."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import re
import sys
from io import StringIO

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
LIVETEST_DIR = SCRIPT_DIR.parent
DEFAULT_LOG = LIVETEST_DIR / "dashboard.log"
DEFAULT_STATE = LIVETEST_DIR / ".dashboard-timing.json"
NOTES_RE = re.compile(r"^(---+|notes:|\[notes\]|##\s*notes)$", re.IGNORECASE)
DURATION_RE = re.compile(r"\s+\d+m\s*$", re.IGNORECASE)
OPEN_STATUSES = ("", "pending", "running", "in progress", "active")


def parse_csv_line(line: str) -> list[str]:
    return next(csv.reader([line]))


def render_csv_row(row: list[str]) -> str:
    out = StringIO()
    writer = csv.writer(out, lineterminator="")
    writer.writerow(row)
    return out.getvalue()


def utc_now() -> tuple[dt.datetime, float]:
    now = dt.datetime.now(dt.timezone.utc)
    return now, now.timestamp()


def parse_time(value: str | None) -> tuple[dt.datetime, float]:
    if not value:
        return utc_now()
    raw = value.strip()
    try:
        epoch = float(raw)
        stamp = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        return stamp, epoch
    except ValueError:
        pass
    normalized = raw.replace("Z", "+00:00")
    stamp = dt.datetime.fromisoformat(normalized)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    stamp = stamp.astimezone(dt.timezone.utc)
    return stamp, stamp.timestamp()


def iso(stamp: dt.datetime) -> str:
    return stamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_text(path: pathlib.Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"dashboard log not found: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def find_matrix(lines: list[str]) -> tuple[int, int, list[str]]:
    header_idx = -1
    header: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        row = parse_csv_line(line)
        if row and row[0].strip().lower() == "milestone":
            header_idx = idx
            header = row
            break
    if header_idx < 0:
        raise SystemExit("dashboard log is missing a milestone CSV header")
    notes_idx = len(lines)
    for idx in range(header_idx + 1, len(lines)):
        if NOTES_RE.match(lines[idx].strip()):
            notes_idx = idx
            break
    return header_idx, notes_idx, header


def find_cell(lines: list[str], lane: str, milestone: str) -> tuple[int, int, list[str]]:
    header_idx, notes_idx, header = find_matrix(lines)
    lane_key = lane.strip().lower()
    lane_idx = -1
    for idx, name in enumerate(header):
        if idx > 0 and name.strip().lower() == lane_key:
            lane_idx = idx
            break
    if lane_idx < 0:
        lanes = ", ".join(h.strip() for h in header[1:])
        raise SystemExit(f"unknown dashboard lane {lane!r}; known lanes: {lanes}")

    milestone_key = milestone.strip().lower()
    for idx in range(header_idx + 1, notes_idx):
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        row = parse_csv_line(raw)
        if row and row[0].strip().lower() == milestone_key:
            while len(row) <= lane_idx:
                row.append("")
            return idx, lane_idx, row
    raise SystemExit(f"unknown dashboard milestone {milestone!r}")


def title_for(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def state_key(log_path: pathlib.Path, title: str, lane: str, milestone: str) -> str:
    return "\u001f".join([
        str(log_path.resolve()),
        title,
        lane.strip().upper(),
        milestone.strip().upper(),
    ])


def load_state(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"version": 1, "starts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"failed to read dashboard timing state {path}: {exc}") from exc
    if not isinstance(data, dict):
        return {"version": 1, "starts": {}}
    data.setdefault("version", 1)
    data.setdefault("starts", {})
    if not isinstance(data["starts"], dict):
        data["starts"] = {}
    return data


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_dashboard(path: pathlib.Path, lines: list[str]) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    text = "\n".join(lines)
    if original.endswith("\n") or not text:
        text += "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def set_cell(log_path: pathlib.Path, lane: str, milestone: str, value: str, *, force: bool = True) -> tuple[str, str]:
    lines = load_text(log_path)
    row_idx, lane_idx, row = find_cell(lines, lane, milestone)
    old = row[lane_idx].strip()
    if not force and old.strip().lower() not in OPEN_STATUSES:
        return old, old
    row[lane_idx] = value
    lines[row_idx] = render_csv_row(row)
    write_dashboard(log_path, lines)
    return old, value


def command_start(args: argparse.Namespace) -> int:
    log_path = pathlib.Path(args.log).expanduser().resolve()
    state_path = pathlib.Path(args.state).expanduser().resolve()
    lines = load_text(log_path)
    stamp, epoch = parse_time(args.at)
    state = load_state(state_path)
    key = state_key(log_path, title_for(lines), args.lane, args.milestone)
    entry = {
        "lane": args.lane.strip().upper(),
        "milestone": args.milestone.strip().upper(),
        "log": str(log_path),
        "started_at": iso(stamp),
        "started_epoch": epoch,
    }
    if not args.no_update:
        old, new = set_cell(log_path, args.lane, args.milestone, args.status, force=args.force)
        if old == new and old.strip().lower() not in OPEN_STATUSES and not args.force:
            print(f"[dashboard-cell] start not recorded; existing closed cell preserved: {args.milestone} {args.lane} = {old}")
            return 0
    state["starts"][key] = entry
    save_state(state_path, state)
    print(f"[dashboard-cell] start {args.milestone} {args.lane} at {iso(stamp)}")
    return 0


def elapsed_minutes(start_epoch: float, end_epoch: float) -> int:
    seconds = max(0.0, end_epoch - start_epoch)
    minutes = int(seconds / 60.0 + 0.5)
    if seconds > 0 and minutes == 0:
        minutes = 1
    return minutes


def command_finish(args: argparse.Namespace) -> int:
    status = " ".join(args.status).strip()
    if not status:
        raise SystemExit("finish requires a status, for example: PASS or PASS-PWN")
    status = DURATION_RE.sub("", status).strip()
    log_path = pathlib.Path(args.log).expanduser().resolve()
    state_path = pathlib.Path(args.state).expanduser().resolve()
    lines = load_text(log_path)
    state = load_state(state_path)
    key = state_key(log_path, title_for(lines), args.lane, args.milestone)
    stamp, end_epoch = parse_time(args.at)
    entry = state.get("starts", {}).pop(key, None)
    if entry and isinstance(entry, dict):
        minutes = elapsed_minutes(float(entry.get("started_epoch", end_epoch)), end_epoch)
        value = f"{status} {minutes}m"
    else:
        value = status
        print(f"[dashboard-cell] warning: no recorded start for {args.milestone} {args.lane}; writing status without duration", file=sys.stderr)
    old, new = set_cell(log_path, args.lane, args.milestone, value, force=True)
    save_state(state_path, state)
    print(f"[dashboard-cell] finish {args.milestone} {args.lane}: {old or '<empty>'} -> {new} at {iso(stamp)}")
    return 0


def command_mark(args: argparse.Namespace) -> int:
    status = " ".join(args.status).strip()
    if not status:
        raise SystemExit("mark requires a status")
    log_path = pathlib.Path(args.log).expanduser().resolve()
    old, new = set_cell(log_path, args.lane, args.milestone, status, force=True)
    print(f"[dashboard-cell] mark {args.milestone} {args.lane}: {old or '<empty>'} -> {new}")
    return 0


def command_reset(args: argparse.Namespace) -> int:
    state_path = pathlib.Path(args.state).expanduser().resolve()
    if args.all:
        save_state(state_path, {"version": 1, "starts": {}})
        print(f"[dashboard-cell] cleared timing state: {state_path}")
        return 0
    if not (args.lane and args.milestone):
        raise SystemExit("reset requires --all or both <lane> <milestone>")
    log_path = pathlib.Path(args.log).expanduser().resolve()
    lines = load_text(log_path)
    state = load_state(state_path)
    key = state_key(log_path, title_for(lines), args.lane, args.milestone)
    removed = state.get("starts", {}).pop(key, None) is not None
    save_state(state_path, state)
    print(f"[dashboard-cell] reset {args.milestone} {args.lane}: {'removed' if removed else 'no recorded start'}")
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", default=os.environ.get("LIVETEST_DASHBOARD_LOG", str(DEFAULT_LOG)), help="dashboard.log path")
    parser.add_argument("--state", default=os.environ.get("LIVETEST_DASHBOARD_TIMING_STATE", str(DEFAULT_STATE)), help="timing state JSON path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track and write live-test dashboard cell timing.")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", aliases=["brief"], help="record cell start time and mark RUNNING")
    add_common(start)
    start.add_argument("lane", help="dashboard lane, e.g. OC, CC, CDX")
    start.add_argument("milestone", help="dashboard milestone, e.g. M2, XP1")
    start.add_argument("--status", default="RUNNING", help="cell text to write while active")
    start.add_argument("--at", help="override timestamp as epoch seconds or ISO-8601 UTC")
    start.add_argument("--no-update", action="store_true", help="record timing without changing dashboard.log")
    start.add_argument("--force", action="store_true", help="overwrite a closed dashboard cell with RUNNING")
    start.set_defaults(func=command_start)

    finish = sub.add_parser("finish", aliases=["grade", "done"], help="write final status with elapsed minutes")
    add_common(finish)
    finish.add_argument("lane")
    finish.add_argument("milestone")
    finish.add_argument("status", nargs=argparse.REMAINDER, help="final status text, e.g. PASS-PWN")
    finish.add_argument("--at", help="override timestamp as epoch seconds or ISO-8601 UTC")
    finish.set_defaults(func=command_finish)

    mark = sub.add_parser("mark", help="write dashboard cell text without timing")
    add_common(mark)
    mark.add_argument("lane")
    mark.add_argument("milestone")
    mark.add_argument("status", nargs=argparse.REMAINDER)
    mark.set_defaults(func=command_mark)

    reset = sub.add_parser("reset", help="clear recorded timing state")
    add_common(reset)
    reset.add_argument("lane", nargs="?")
    reset.add_argument("milestone", nargs="?")
    reset.add_argument("--all", action="store_true", help="clear all recorded starts")
    reset.set_defaults(func=command_reset)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
