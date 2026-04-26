from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


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
