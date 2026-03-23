#!/usr/bin/env python3
"""Claude Code hooks — auto-provision instance from project root, then run hook."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _auto_provision_if_needed() -> None:
    """Derive QUAID_INSTANCE from the CC project root and provision if needed.

    Uses the adapter's get_instance_name() which reads CLAUDE_PROJECT_DIR —
    the env var CC injects for all hooks and Bash tool calls. This is the
    stable project root regardless of the shell's current working directory.

    Skips only if QUAID_INSTANCE is already set in the environment.
    """
    if os.environ.get("QUAID_INSTANCE", "").strip():
        return

    try:
        from adaptors.claude_code.adapter import ClaudeCodeAdapter
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        adapter = ClaudeCodeAdapter()
        name = adapter.get_instance_name()
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
