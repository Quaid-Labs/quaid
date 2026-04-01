"""Architecture boundary tests for extraction execution paths."""

from __future__ import annotations

import ast
from pathlib import Path


def test_only_daemon_calls_run_extract_from_transcript():
    """Enforce daemon-only deep extraction execution.

    The only runtime caller of core.ingest_runtime.run_extract_from_transcript()
    should be core/extraction_daemon.py. Hooks/adapters must enqueue signals.
    """
    repo_root = Path(__file__).resolve().parents[1]
    allowed_runtime_callers = {
        repo_root / "core" / "extraction_daemon.py",
    }
    violations: list[str] = []

    for py_file in repo_root.rglob("*.py"):
        if "/tests/" in py_file.as_posix():
            continue
        if py_file.name == "__init__.py":
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except Exception as exc:
            violations.append(f"{py_file}: failed to parse ({exc})")
            continue

        for node in ast.walk(tree):
            # import guard: from core.ingest_runtime import run_extract_from_transcript
            if isinstance(node, ast.ImportFrom) and node.module == "core.ingest_runtime":
                for alias in node.names:
                    if alias.name == "run_extract_from_transcript" and py_file not in allowed_runtime_callers:
                        violations.append(
                            f"{py_file}:{node.lineno} imports run_extract_from_transcript outside daemon"
                        )

            # call guard: run_extract_from_transcript(...)
            if isinstance(node, ast.Call):
                fn = node.func
                called_name = ""
                if isinstance(fn, ast.Name):
                    called_name = fn.id
                elif isinstance(fn, ast.Attribute):
                    called_name = fn.attr
                if called_name == "run_extract_from_transcript" and py_file not in allowed_runtime_callers:
                    violations.append(
                        f"{py_file}:{node.lineno} calls run_extract_from_transcript outside daemon"
                    )

    assert not violations, (
        "Boundary violation: only core/extraction_daemon.py may call "
        "run_extract_from_transcript.\n"
        + "\n".join(sorted(violations))
    )

