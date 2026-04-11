#!/usr/bin/env python3
"""Claude Code hooks — async first-touch auto-provision for project silos."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_PROVISION_MARKER_TTL_SECONDS = 15 * 60
_PROVISION_NOTICE = (
    "Quaid is setting up memory for this project. This happens once and takes about a minute. "
    "Memory features will be available on your next message."
)


def _resolve_provision_target() -> tuple:
    from adaptors.claude_code.adapter import ClaudeCodeAdapter
    from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

    adapter = ClaudeCodeAdapter()
    mgr = ClaudeCodeInstanceManager(adapter)
    name = adapter.get_instance_name()
    instance_id = mgr.resolve_instance_id(name)
    silo_root = adapter.quaid_home() / "instances" / instance_id
    config_path = silo_root / "config.json"
    marker_path = silo_root / ".runtime" / "provisioning.json"
    return adapter, mgr, name, instance_id, silo_root, config_path, marker_path


def _read_marker(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _marker_is_active(path: Path) -> bool:
    if not path.is_file():
        return False
    payload = _read_marker(path)
    started = float(payload.get("started_at", 0.0) or 0.0)
    if started <= 0:
        started = path.stat().st_mtime
    if (time.time() - started) <= _PROVISION_MARKER_TTL_SECONDS:
        return True
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def _write_marker(path: Path, payload: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
        return True
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _claim_provision_slot(path: Path, instance_id: str) -> bool:
    if _marker_is_active(path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "instance_id": instance_id,
            "started_at": time.time(),
            "pid": os.getpid(),
            "status": "starting",
        },
        indent=2,
    ) + "\n"
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, payload.encode("utf-8"))
        return True
    finally:
        os.close(fd)


def _queue_async_notice(instance_id: str) -> None:
    previous = os.environ.get("QUAID_INSTANCE")
    try:
        os.environ["QUAID_INSTANCE"] = instance_id
        from lib.runtime_context import queue_deferred_notice

        queue_deferred_notice(
            _PROVISION_NOTICE,
            kind="setup",
            priority="normal",
            source="claude_code_auto_provision",
            dedupe_key=f"cc-auto-provision:{instance_id}",
        )
    finally:
        if previous is None:
            os.environ.pop("QUAID_INSTANCE", None)
        else:
            os.environ["QUAID_INSTANCE"] = previous


def _spawn_background_provision(name: str, instance_id: str, marker_path: Path) -> None:
    log_path = marker_path.parent.parent / "logs" / "provision.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["QUAID_INSTANCE"] = instance_id
    with open(log_path, "a", encoding="utf-8") as log_file:
        subprocess.Popen(
            [sys.executable, __file__, "_background_provision", name, instance_id, str(marker_path)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )


def _run_background_provision(name: str, instance_id: str, marker_path: str) -> int:
    marker = Path(marker_path)
    previous = os.environ.get("QUAID_INSTANCE")
    try:
        os.environ["QUAID_INSTANCE"] = instance_id
        from adaptors.claude_code.adapter import ClaudeCodeAdapter
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        from core.extraction_daemon import ensure_alive

        adapter = ClaudeCodeAdapter()
        mgr = ClaudeCodeInstanceManager(adapter)
        mgr.auto_provision(name)

        session_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if session_token:
            adapter.store_auth_token(session_token)

        ensure_alive()
        _write_marker(
            marker,
            {
                "instance_id": instance_id,
                "started_at": _read_marker(marker).get("started_at", time.time()),
                "completed_at": time.time(),
                "pid": os.getpid(),
                "status": "complete",
            },
        )
        marker.unlink(missing_ok=True)
        return 0
    except Exception as exc:
        try:
            _write_marker(
                marker,
                {
                    "instance_id": instance_id,
                    "started_at": _read_marker(marker).get("started_at", time.time()),
                    "failed_at": time.time(),
                    "pid": os.getpid(),
                    "status": "failed",
                    "error": str(exc),
                },
            )
            from lib.runtime_context import queue_deferred_notice

            queue_deferred_notice(
                f"Quaid failed to finish setup for this project. Memory features remain unavailable until setup succeeds again. Error: {exc}",
                kind="setup",
                priority="high",
                source="claude_code_auto_provision",
                dedupe_key=f"cc-auto-provision-failed:{instance_id}",
            )
        except Exception:
            pass
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"[quaid] Background auto-provision failed for {instance_id}: {exc}", file=sys.stderr)
        return 1
    finally:
        if previous is None:
            os.environ.pop("QUAID_INSTANCE", None)
        else:
            os.environ["QUAID_INSTANCE"] = previous


def _auto_provision_if_needed() -> bool:
    """Start async silo provisioning when this project has no instance yet.

    Returns True when the hook should continue into the normal core path.
    Returns False when provisioning is pending/running and the current hook
    should exit fast so the first Claude reply is not blocked.
    """
    try:
        _adapter, _mgr, name, instance_id, _silo_root, config_path, marker_path = _resolve_provision_target()
        if config_path.is_file():
            return True
        if _marker_is_active(marker_path):
            print(f"[quaid] Auto-provision already running for {instance_id}", file=sys.stderr)
            return False
        if _claim_provision_slot(marker_path, instance_id):
            _queue_async_notice(instance_id)
            _spawn_background_provision(name, instance_id, marker_path)
            print(f"[quaid] Started async auto-provision for {instance_id}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[quaid] Auto-provision failed: {e}", file=sys.stderr)
        return True


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "_background_provision":
        if len(sys.argv) != 5:
            print("[quaid] invalid background provision args", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(_run_background_provision(sys.argv[2], sys.argv[3], sys.argv[4]))
    if not _auto_provision_if_needed():
        return
    from core.interface.hooks import main as _main
    _main()


if __name__ == "__main__":
    main()
