from pathlib import Path
import re


_CONDITIONAL_PLATFORM_RE = re.compile(
    r"\b(?:if|elif|return)\b.*(?:claude-code|codex|openclaw|standalone)"
)


def test_shared_layers_do_not_add_new_hardcoded_platform_conditionals():
    repo_root = Path(__file__).resolve().parent.parent
    scan_roots = [repo_root / "core", repo_root / "facade", repo_root / "orchestrator"]
    allowed = {
        ("core/extraction_daemon.py", 'if instance.startswith("claude-code-") or instance == "claude-code":'),
        ("core/extraction_daemon.py", 'elif instance.startswith("openclaw-") or instance == "openclaw":'),
        ("core/extraction_daemon.py", 'elif instance.startswith("codex-") or instance == "codex":'),
        ("core/extraction_daemon.py", 'elif instance.startswith("standalone-") or instance == "standalone":'),
        ("core/extraction_daemon.py", 'return adapter_name in ("openclaw",)'),
        ("core/interface/hooks.py", 'if _current_adapter_id() == "codex":'),
        ("core/interface/hooks.py", 'pattern = f"rollout-*{session_id}.jsonl" if adapter_id == "codex" else f"{session_id}.jsonl"'),
        ("core/interface/hooks.py", 'if hook_cwd and sessions_dir and adapter_id == "claude-code":'),
        ("core/interface/hooks.py", 'if adapter_id == "codex":'),
        ("core/interface/hooks.py", 'if sessions_dir and adapter_id in ("openclaw", "standalone", ""):'),
        ("core/interface/hooks.py", 'elif args.command == "codex-stop":'),
    }
    found = []
    for root in scan_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(repo_root).as_posix()
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if _CONDITIONAL_PLATFORM_RE.search(stripped):
                    if (rel, stripped) not in allowed:
                        found.append(f"{rel}: {stripped}")
    assert not found, "New hardcoded platform conditionals found in shared layers:\n" + "\n".join(sorted(found))
