#!/usr/bin/env python3
"""Interactive config CLI for Quaid memory config."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def _workspace_root() -> Path:
    for env in ("QUAID_HOME", "CLAWDBOT_WORKSPACE"):
        value = os.getenv(env, "").strip()
        if value:
            return Path(value)
    return Path.cwd()


def _config_path() -> Path:
    root = _workspace_root()
    return root / "config" / "memory.json"


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def _get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Any = data
    for seg in parts[:-1]:
        if not isinstance(cur, dict):
            raise ValueError(f"Cannot set {dotted}: parent is not an object")
        if seg not in cur or not isinstance(cur[seg], dict):
            cur[seg] = {}
        cur = cur[seg]
    if not isinstance(cur, dict):
        raise ValueError(f"Cannot set {dotted}: parent is not an object")
    cur[parts[-1]] = value


def _print_summary(path: Path, data: dict[str, Any]) -> None:
    print("Quaid Configuration")
    print(str(path))
    print()

    print(f"provider:         {_get(data, 'models.llmProvider', 'default')}")
    print(f"deep reasoning:   {_get(data, 'models.deepReasoning', 'default')}")
    print(f"fast reasoning:    {_get(data, 'models.fastReasoning', 'default')}")
    print(f"embeddings model: {_get(data, 'models.embeddings', 'default')}")
    print(f"notify level:     {_get(data, 'notifications.level', 'normal')}")
    print(f"fail hard:        {_get(data, 'retrieval.failHard', _get(data, 'retrieval.fail_hard', True))}")
    print(f"janitor parallel: {_get(data, 'janitor.parallel.enabled', True)}")
    print(f"llm workers:      {_get(data, 'janitor.parallel.llmWorkers', _get(data, 'janitor.parallel.llm_workers', 4))}")
    print(f"prepass workers:  {_get(data, 'janitor.parallel.lifecyclePrepassWorkers', _get(data, 'janitor.parallel.lifecycle_prepass_workers', 3))}")
    print(f"idle timeout:     {_get(data, 'capture.idle_timeout_minutes', 10)}m")
    print()
    print("systems:")
    for key in ("memory", "journal", "projects", "workspace"):
        enabled = bool(_get(data, f"systems.{key}", True))
        print(f"  {key:<10} {'on' if enabled else 'off'}")


def _prompt_str(label: str, current: str) -> str:
    value = input(f"{label} [{current}]: ").strip()
    return value if value else current


def _prompt_int(label: str, current: int) -> int:
    raw = input(f"{label} [{current}]: ").strip()
    if not raw:
        return current
    return int(raw)


def _toggle_bool(data: dict[str, Any], key: str) -> None:
    current = bool(_get(data, key, True))
    _set(data, key, not current)


def _edit_systems(data: dict[str, Any]) -> None:
    while True:
        print("\nSystems")
        for idx, key in enumerate(("memory", "journal", "projects", "workspace"), start=1):
            enabled = bool(_get(data, f"systems.{key}", True))
            print(f"{idx}. {key:<10} {'on' if enabled else 'off'}")
        print("5. Back")
        choice = input("Select: ").strip()
        if choice == "1":
            _toggle_bool(data, "systems.memory")
        elif choice == "2":
            _toggle_bool(data, "systems.journal")
        elif choice == "3":
            _toggle_bool(data, "systems.projects")
        elif choice == "4":
            _toggle_bool(data, "systems.workspace")
        elif choice == "5":
            return
        else:
            print("Invalid choice")


def interactive_edit(path: Path, data: dict[str, Any]) -> bool:
    staged = json.loads(json.dumps(data))

    while True:
        print("\nQuaid Config Editor")
        print("1. LLM provider")
        print("2. Deep reasoning model")
        print("3. Fast reasoning model")
        print("4. Embeddings model")
        print("5. Notification level")
        print("6. Idle timeout (minutes)")
        print("7. Fail hard (retrieval.failHard)")
        print("8. Janitor parallel enabled")
        print("9. Janitor LLM workers")
        print("10. Janitor prepass workers")
        print("11. Systems on/off")
        print("12. Show summary")
        print("13. Save and exit")
        print("0. Exit without saving")
        choice = input("Select: ").strip()

        try:
            if choice == "1":
                cur = str(_get(staged, "models.llmProvider", "default"))
                _set(staged, "models.llmProvider", _prompt_str("models.llmProvider", cur))
            elif choice == "2":
                cur = str(_get(staged, "models.deepReasoning", "default"))
                _set(staged, "models.deepReasoning", _prompt_str("models.deepReasoning", cur))
            elif choice == "3":
                cur = str(_get(staged, "models.fastReasoning", "default"))
                _set(staged, "models.fastReasoning", _prompt_str("models.fastReasoning", cur))
            elif choice == "4":
                cur = str(_get(staged, "models.embeddings", "default"))
                _set(staged, "models.embeddings", _prompt_str("models.embeddings", cur))
            elif choice == "5":
                cur = str(_get(staged, "notifications.level", "normal"))
                _set(staged, "notifications.level", _prompt_str("notifications.level", cur))
            elif choice == "6":
                cur = int(_get(staged, "capture.idle_timeout_minutes", 10))
                _set(staged, "capture.idle_timeout_minutes", _prompt_int("capture.idle_timeout_minutes", cur))
            elif choice == "7":
                cur = bool(_get(staged, "retrieval.failHard", _get(staged, "retrieval.fail_hard", True)))
                raw = _prompt_str("retrieval.failHard (true/false)", "true" if cur else "false").lower()
                _set(staged, "retrieval.failHard", raw in {"1", "true", "yes", "on"})
            elif choice == "8":
                cur = bool(_get(staged, "janitor.parallel.enabled", True))
                raw = _prompt_str("janitor.parallel.enabled (true/false)", "true" if cur else "false").lower()
                _set(staged, "janitor.parallel.enabled", raw in {"1", "true", "yes", "on"})
            elif choice == "9":
                cur = int(_get(staged, "janitor.parallel.llmWorkers", _get(staged, "janitor.parallel.llm_workers", 4)))
                _set(staged, "janitor.parallel.llmWorkers", _prompt_int("janitor.parallel.llmWorkers", cur))
            elif choice == "10":
                cur = int(_get(staged, "janitor.parallel.lifecyclePrepassWorkers", _get(staged, "janitor.parallel.lifecycle_prepass_workers", 3)))
                _set(staged, "janitor.parallel.lifecyclePrepassWorkers", _prompt_int("janitor.parallel.lifecyclePrepassWorkers", cur))
            elif choice == "11":
                _edit_systems(staged)
            elif choice == "12":
                _print_summary(path, staged)
            elif choice == "13":
                _save_config(path, staged)
                print(f"Saved: {path}")
                return True
            elif choice == "0":
                print("No changes saved")
                return False
            else:
                print("Invalid choice")
        except ValueError as err:
            print(f"Invalid value: {err}")


def parse_literal(raw: str) -> Any:
    value = raw.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        if value.startswith("{") or value.startswith("["):
            return json.loads(value)
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Quaid config helper")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("show", help="Show summary")
    sub.add_parser("path", help="Print config path")
    sub.add_parser("edit", help="Interactive config editor")

    set_p = sub.add_parser("set", help="Set a dotted key path")
    set_p.add_argument("key", help="Dotted path (e.g. models.fastReasoning)")
    set_p.add_argument("value", help="Value (string/number/true/false/json)")

    args = parser.parse_args()
    cmd = args.cmd or "show"

    path = _config_path()
    if cmd == "path":
        print(str(path))
        return 0

    try:
        data = _load_config(path)
    except Exception as err:
        print(str(err))
        return 1

    if cmd == "show":
        _print_summary(path, data)
        return 0

    if cmd == "edit":
        interactive_edit(path, data)
        return 0

    if cmd == "set":
        _set(data, args.key, parse_literal(args.value))
        _save_config(path, data)
        print(f"Set {args.key} in {path}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
