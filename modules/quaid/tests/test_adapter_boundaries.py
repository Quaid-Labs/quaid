"""Adapter boundary checks for core/lib code."""

import re
from pathlib import Path


BANNED_ADAPTER_IDS = {"codex", "claude-code", "openclaw"}


def test_core_lib_do_not_hardcode_adapter_type_literals() -> None:
    """Core/lib code must consume adapter config, not branch on adapter ids."""
    repo = Path(__file__).resolve().parents[1]
    roots = [repo / "lib", repo / "core"]
    violations: list[str] = []

    literal_re = re.compile(r"""(["'])(codex|claude-code|openclaw)\1""")
    for root in roots:
        for path in sorted(
            p for suffix in ("*.py", "*.ts", "*.js") for p in root.rglob(suffix)
        ):
            text = path.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), start=1):
                for match in literal_re.finditer(line):
                    rel = path.relative_to(repo)
                    violations.append(f"{rel}:{idx}:{match.start() + 1} {match.group(0)}")

    assert violations == []
