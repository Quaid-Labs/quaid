#!/usr/bin/env python3
"""CLI for project documentation supervisor operations."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import project_docs


def cmd_update(args) -> None:
    try:
        prior_state = project_docs.read_state(args.project)
        req = project_docs.request_update(args.project, reason="manual", requested_by="cli")
        pid = project_docs.ensure_supervisor_alive()
        final_state = None
        if args.wait:
            final_state = project_docs.wait_for_request(args.project, req["request_id"], timeout_seconds=args.timeout)
    except (KeyError, ValueError, TimeoutError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    out = {"queued": True, "request": req, "supervisor_pid": pid, "prior_state": prior_state}
    if final_state is not None:
        out["final_state"] = final_state
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        if prior_state.get("last_error"):
            print(f"Previous project docs update error: {prior_state.get('last_error')}", file=sys.stderr)
        print(f"Queued project docs update for {args.project}")
        print(f"  request: {req['request_id']}")
        print(f"  supervisor: {pid}")
        if final_state is not None:
            print(f"  final status: {final_state.get('status', 'unknown')}")
            if final_state.get("last_error"):
                print(f"  last error: {final_state.get('last_error')}")


def cmd_status(args) -> None:
    try:
        status = project_docs.project_status(args.project)
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(project_docs.format_status(status))


def cmd_diff(args) -> None:
    try:
        diff = project_docs.project_diff(args.project, full=bool(args.full))
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(diff, indent=2))
    else:
        print(project_docs.format_diff(diff))


def cmd_supervisor(args) -> None:
    if args.action == "ensure":
        print(project_docs.ensure_supervisor_alive())
    elif args.action == "stop":
        print("stopped" if project_docs.stop_supervisor() else "not running")
    else:
        pid = project_docs.read_supervisor_pid()
        print(pid if pid else "not running")


def main() -> None:
    parser = argparse.ArgumentParser(description="Quaid project docs supervisor CLI")
    parser.add_argument("--json", action="store_true", help="JSON output")
    sub = parser.add_subparsers(dest="command")

    update_p = sub.add_parser("update", help="Queue an async project docs update")
    update_p.add_argument("project")
    update_p.add_argument("--wait", action="store_true", help="Wait until this request is applied or fails")
    update_p.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait with --wait")

    status_p = sub.add_parser("status", help="Show project docs freshness/status")
    status_p.add_argument("project")

    diff_p = sub.add_parser("diff", help="Show pending project docs update diff")
    diff_p.add_argument("project")
    diff_p.add_argument("--stat", action="store_true", help="Show compact diff/stat output")
    diff_p.add_argument("--full", action="store_true", help="Show full git diff")

    sup_p = sub.add_parser("supervisor", help="Manage the Quaid supervisor")
    sup_p.add_argument("action", choices=("status", "ensure", "stop"), nargs="?", default="status")

    args = parser.parse_args()
    if args.command == "update":
        cmd_update(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "diff":
        cmd_diff(args)
    elif args.command == "supervisor":
        cmd_supervisor(args)
    else:
        parser.print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
