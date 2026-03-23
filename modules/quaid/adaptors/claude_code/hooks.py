#!/usr/bin/env python3
"""Claude Code hooks — auto-provision silo from project root, then run hook."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _auto_provision_if_needed() -> None:
    """Create a Quaid silo for this project if one does not exist yet.

    get_adapter() (called by core.interface.hooks) bootstraps QUAID_INSTANCE
    via adapter.get_instance_name() — this function only needs to ensure the
    silo directory exists before the hook runs.
    """
    try:
        from adaptors.claude_code.adapter import ClaudeCodeAdapter
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        adapter = ClaudeCodeAdapter()
        name = adapter.get_instance_name()
        mgr = ClaudeCodeInstanceManager(adapter)
        instance_id, was_new = mgr.auto_provision(name)

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
