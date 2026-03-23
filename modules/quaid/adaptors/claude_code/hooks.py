#!/usr/bin/env python3
"""Claude Code hooks — auto-provision instance from PWD, then run hook."""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _auto_provision_if_needed() -> None:
    """Derive QUAID_INSTANCE from PWD and provision a silo if not yet created.

    Instance name is the full project path translated to a folder-safe slug.
    The home prefix is stripped so ~/work/myapp → work-myapp.
    Home dir itself → home. Paths outside home use the full path from root.

    Skips only if QUAID_INSTANCE is already set in the environment.
    """
    if os.environ.get("QUAID_INSTANCE", "").strip():
        return

    cwd = Path(os.getcwd()).resolve()
    home = Path.home().resolve()

    # Translate the full absolute path into a folder-safe slug.
    # /Users/owner/work/myapp → users-clawdbot-work-myapp
    slug = re.sub(r"[^a-z0-9]+", "-", str(cwd).lower()).strip("-")
    name = slug

    try:
        from adaptors.claude_code.adapter import ClaudeCodeAdapter
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        adapter = ClaudeCodeAdapter()
        mgr = ClaudeCodeInstanceManager(adapter)
        instance_id, was_new = mgr.auto_provision(name)
        os.environ["QUAID_INSTANCE"] = instance_id

        if was_new:
            adapter.notify(
                f"New Quaid instance provisioned for this project: {instance_id}"
            )
            print(f"[quaid] Auto-provisioned instance: {instance_id}", file=sys.stderr)
    except Exception as e:
        print(f"[quaid] Auto-provision failed: {e}", file=sys.stderr)


def main():
    _auto_provision_if_needed()
    from core.interface.hooks import main as _main
    _main()


if __name__ == "__main__":
    main()
