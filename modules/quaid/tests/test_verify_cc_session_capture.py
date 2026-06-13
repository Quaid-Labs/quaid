import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "livetest"
    / "scripts"
    / "verify-cc-session-capture.sh"
)


def _project_slug(project_dir: Path) -> str:
    resolved = str(project_dir.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    readable = re.sub(r"[^a-z0-9]+", "-", project_dir.resolve().name.lower()).strip("-") or "project"
    readable = readable[:39].strip("-") or "project"
    return f"{readable}-{digest}"


def _prepare_capture_home(tmp_path: Path, project_dir: Path, instance_id: str) -> Path:
    home = tmp_path / "home"
    global_settings = home / ".claude" / "settings.json"
    global_settings.parent.mkdir(parents=True)
    global_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [],
                    "UserPromptSubmit": [],
                    "PreCompact": [],
                    "SessionEnd": [],
                }
            }
        ),
        encoding="utf-8",
    )

    session_dir = home / ".claude" / "projects" / str(project_dir.resolve()).replace("/", "-")
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text('{"type":"message"}\n', encoding="utf-8")

    trace_path = home / ".quaid" / "instances" / instance_id / "logs" / "quaid-hook-trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text('{"event":"hook"}\n', encoding="utf-8")
    (home / ".quaid" / "instances" / instance_id / "config.json").write_text(
        json.dumps({"id": instance_id, "adapter": {"type": "claude-code"}}),
        encoding="utf-8",
    )
    return home


def _run_verify(home: Path, project_dir: Path, instance_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--remote",
            "localhost",
            "--project-dir",
            str(project_dir),
            "--instance",
            instance_id,
            "--max-age-min",
            "60",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_verify_cc_session_capture_accepts_bound_instance_when_hash_slug_differs(tmp_path):
    project_dir = tmp_path / "cc-livetest"
    project_dir.mkdir()
    instance_id = "claude-code-private-tmp-cc-livetest"
    home = _prepare_capture_home(tmp_path, project_dir, instance_id)

    binding_path = (
        home
        / ".quaid"
        / "shared"
        / "instance-bindings"
        / "claude-code"
        / f"{_project_slug(project_dir)}.json"
    )
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(
        json.dumps({"instance": instance_id, "project_dir": str(project_dir.resolve())}),
        encoding="utf-8",
    )

    result = _run_verify(home, project_dir, instance_id)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"binding_instance={instance_id}" in result.stdout
    assert "PASS" in result.stdout


def test_verify_cc_session_capture_still_fails_hash_slug_mismatch_without_binding(tmp_path):
    project_dir = tmp_path / "cc-livetest"
    project_dir.mkdir()
    instance_id = "claude-code-private-tmp-cc-livetest"
    home = _prepare_capture_home(tmp_path, project_dir, instance_id)

    result = _run_verify(home, project_dir, instance_id)

    assert result.returncode == 1
    assert "binding_instance=(none)" in result.stdout
    assert "path-derived instance mismatch" in result.stdout
