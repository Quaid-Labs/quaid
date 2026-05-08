from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def _instance_slug(project_dir: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(project_dir.resolve()).lower()).strip("-")


def test_quaid_cli_derives_openclaw_instance_from_agent_workspace(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    visible_home = home / "quaid"
    openclaw_root = home / ".openclaw"
    agent_workspace = home / "openclaw-m13test"

    (openclaw_root / "agents" / "m13test" / "agent").mkdir(parents=True, exist_ok=True)
    agent_workspace.mkdir(parents=True, exist_ok=True)
    (quaid_home / "instances" / "openclaw-livetest").mkdir(parents=True, exist_ok=True)
    (quaid_home / "instances" / "openclaw-m13test").mkdir(parents=True, exist_ok=True)

    (openclaw_root / "openclaw.json").write_text(
        json.dumps(
            {
                "env": {
                    "vars": {
                        "QUAID_HOME": str(quaid_home),
                        "OPENCLAW_WORKSPACE": str(quaid_home),
                        "QUAID_INSTANCE": "openclaw-livetest",
                    },
                    "QUAID_HOME": str(quaid_home),
                    "QUAID_INSTANCE": "openclaw-livetest",
                },
                "agents": {
                    "list": [
                        {"id": "main", "default": True, "name": "Default"},
                        {
                            "id": "m13test",
                            "name": "m13test",
                            "workspace": str(agent_workspace),
                            "agentDir": str(openclaw_root / "agents" / "m13test" / "agent"),
                        },
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "OPENCLAW_WORKSPACE": str(quaid_home),
        "QUAID_VISIBLE_HOME": str(visible_home),
        "QUAID_INSTANCE": "openclaw-livetest",
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [str(quaid_bin), "config", "path"],
        cwd=agent_workspace,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip().endswith("/instances/openclaw-m13test/config.json")


def test_quaid_project_create_links_claude_code_instance_from_safe_cwd(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    visible_home = home / "quaid"
    short_root = Path(tempfile.mkdtemp(prefix="qcli-", dir="/tmp"))
    project_dir = short_root / "cc-livetest"
    project_dir.mkdir(parents=True)

    instance = f"claude-code-{_instance_slug(project_dir)}"
    instance_dir = quaid_home / "instances" / instance
    instance_dir.mkdir(parents=True)
    (instance_dir / "config.json").write_text(
        json.dumps({"adapter": {"type": "claude-code"}}) + "\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_VISIBLE_HOME": str(visible_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    env.pop("QUAID_INSTANCE", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("CODEX_PROJECT_DIR", None)
    env.pop("QUAID_ADAPTER_TYPE", None)

    try:
        subprocess.run(
            [str(quaid_bin), "project", "create", "livetest-agentmsg-xp"],
            cwd=project_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(short_root, ignore_errors=True)

    registry = json.loads((quaid_home / "project-registry.json").read_text(encoding="utf-8"))
    assert registry["projects"]["livetest-agentmsg-xp"]["instances"] == [instance]


def test_quaid_cli_does_not_guess_ambiguous_project_cwd(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    visible_home = home / "quaid"
    short_root = Path(tempfile.mkdtemp(prefix="qcli-", dir="/tmp"))
    project_dir = short_root / "shared-project"
    project_dir.mkdir(parents=True)

    slug = _instance_slug(project_dir)
    for instance, adapter in (
        (f"claude-code-{slug}", "claude-code"),
        (f"codex-{slug}", "codex"),
    ):
        instance_dir = quaid_home / "instances" / instance
        instance_dir.mkdir(parents=True)
        (instance_dir / "config.json").write_text(
            json.dumps({"adapter": {"type": adapter}}) + "\n",
            encoding="utf-8",
        )

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_VISIBLE_HOME": str(visible_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    env.pop("QUAID_INSTANCE", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("CODEX_PROJECT_DIR", None)
    env.pop("QUAID_ADAPTER_TYPE", None)

    try:
        result = subprocess.run(
            [str(quaid_bin), "project", "create", "ambiguous-project"],
            cwd=project_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(short_root, ignore_errors=True)

    assert "Created project: ambiguous-project" in result.stdout
    registry = json.loads((quaid_home / "project-registry.json").read_text(encoding="utf-8"))
    assert registry["projects"]["ambiguous-project"]["instances"] == []


def test_quaid_cli_ignores_stale_inherited_quaid_home_when_installed_home_exists(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    installed_home = home / ".quaid"
    plugin_dir = installed_home / "plugins" / "quaid"
    visible_home = home / "quaid"
    plugin_dir.mkdir(parents=True)
    (installed_home / "shared").mkdir(parents=True)

    installed_quaid = plugin_dir / "quaid"
    installed_quaid.write_text(quaid_bin.read_text(encoding="utf-8"), encoding="utf-8")
    installed_quaid.chmod(0o755)

    stale_home = tmp_path / "missing-user" / ".quaid"
    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(stale_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [str(installed_quaid), "config"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "cd:" not in result.stderr
    assert str(installed_home / "shared" / "config" / "global" / "config.json") in result.stderr
    assert str(visible_home) not in result.stderr


def test_quaid_session_expand_microchunk_cli(tmp_path: Path, monkeypatch) -> None:
    from datastore.sessiondb import session_store

    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    quaid_home.mkdir(parents=True)
    session_db = tmp_path / "session.db"
    monkeypatch.setenv("SESSION_DB_PATH", str(session_db))

    stored = session_store.store_session_source_text(
        text="User: Mira keeps the ferry receipt in the red notebook.\nAssistant: Noted.",
        owner_id="owner-cli",
        session_id="sess-cli",
        source_id="source-cli",
        max_microchunk_tokens=16,
    )
    microchunk_id = stored["microchunks"][0]["microchunk_id"]

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "SESSION_DB_PATH": str(session_db),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [str(quaid_bin), "session", "expand-microchunk", microchunk_id, "--owner", "owner-cli"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "expanded_pair:" in result.stdout
    assert "User: Mira keeps the ferry receipt in the red notebook." in result.stdout
    assert "Assistant: Noted." in result.stdout
