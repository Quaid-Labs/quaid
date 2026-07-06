from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


def _instance_slug(project_dir: Path) -> str:
    from lib.instance import instance_slug_from_project_dir

    return instance_slug_from_project_dir(str(project_dir))


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


def test_quaid_project_create_reads_legacy_claude_code_binding_from_safe_cwd(tmp_path: Path) -> None:
    from lib.instance import _legacy_instance_slug_from_project_dir

    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    visible_home = home / "quaid"
    short_root = Path(tempfile.mkdtemp(prefix="qcli-", dir="/tmp"))
    project_dir = short_root / "cc_project"
    project_dir.mkdir(parents=True)

    instance = "claude-code-explicit-alpha"
    instance_dir = quaid_home / "instances" / instance
    instance_dir.mkdir(parents=True)
    (instance_dir / "config.json").write_text(
        json.dumps({"adapter": {"type": "claude-code"}}) + "\n",
        encoding="utf-8",
    )
    legacy_slug = _legacy_instance_slug_from_project_dir(str(project_dir))
    binding_path = quaid_home / "shared" / "instance-bindings" / "claude-code" / f"{legacy_slug}.json"
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(
        json.dumps(
            {
                "adapter": "claude-code",
                "instance": instance,
                "project_dir": str(project_dir.resolve()),
            }
        )
        + "\n",
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
            [str(quaid_bin), "project", "create", "legacy-bound-project"],
            cwd=project_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(short_root, ignore_errors=True)

    registry = json.loads((quaid_home / "project-registry.json").read_text(encoding="utf-8"))
    assert registry["projects"]["legacy-bound-project"]["instances"] == [instance]


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


def test_quaid_instances_prune_stale_openclaw_requires_force(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }

    result = subprocess.run(
        [str(quaid_bin), "instances", "prune-stale-openclaw"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "without --force" in result.stderr


def test_quaid_instances_prune_stale_openclaw_removes_deleted_agent_silo(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    visible_home = home / "quaid"
    openclaw_root = home / ".openclaw"
    stale = quaid_home / "instances" / "openclaw-m13test"

    (openclaw_root).mkdir(parents=True)
    (openclaw_root / "openclaw.json").write_text(
        json.dumps({"agents": {"list": [{"id": "main"}]}}),
        encoding="utf-8",
    )
    stale.mkdir(parents=True)
    (stale / "config.json").write_text(
        json.dumps({"adapter": {"type": "openclaw"}}) + "\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_VISIBLE_HOME": str(visible_home),
        "OPENCLAW_CONFIG_PATH": str(openclaw_root / "openclaw.json"),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }

    result = subprocess.run(
        [str(quaid_bin), "instances", "prune-stale-openclaw", "--force", "--json"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["pruned"] == ["openclaw-m13test"]
    assert not stale.exists()


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


def test_quaid_session_expand_chunk_cli(tmp_path: Path, monkeypatch) -> None:
    from datastore.memorydb.memory_graph import MemoryGraph

    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    quaid_home.mkdir(parents=True)
    memory_db = tmp_path / "memory.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(memory_db))

    graph = MemoryGraph(db_path=memory_db)
    rows = graph.store_session_chunks(
        [
            "User: Mira bought ferry tickets at the Lisbon kiosk.",
            "Assistant: The receipt is in the red notebook.",
            "User: The return time is written beside the map.",
        ],
        owner_id="owner-cli",
        session_id="sess-cli",
        source_id="sess-cli",
    )
    chunk_id = rows[1]["chunk_id"]

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "MEMORY_DB_PATH": str(memory_db),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [
            str(quaid_bin),
            "session",
            "expand-chunk",
            chunk_id,
            "--owner",
            "owner-cli",
            "--before",
            "1",
            "--after",
            "1",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "chunk_id:" in result.stdout
    assert "window:" in result.stdout
    assert "User: Mira bought ferry tickets at the Lisbon kiosk." in result.stdout
    assert "Assistant: The receipt is in the red notebook." in result.stdout
    assert "User: The return time is written beside the map." in result.stdout


def test_quaid_session_expand_chunk_json_breaks_result_cycles(monkeypatch, capsys) -> None:
    from core import session_cli

    row = {
        "chunk_id": "chunk-cycle",
        "session_id": "sess-cycle",
        "chunk_index": 0,
        "text": "cycle-safe output",
    }
    row["self"] = row

    class FakeBridge:
        def get_session_chunk(self, chunk_id: str, *, owner_id: str, before: int = 0, after: int = 0):
            assert chunk_id == "chunk-cycle"
            assert owner_id
            return row

    monkeypatch.setattr(session_cli, "get_session_memory_bridge", lambda: FakeBridge())

    assert session_cli.main(["expand-chunk", "chunk-cycle", "--owner", "owner-cli", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chunk_id"] == "chunk-cycle"
    assert payload["self"] == "[Circular]"


def test_quaid_session_expand_chunk_json_stringifies_non_json_scalars(monkeypatch, capsys) -> None:
    from core import session_cli

    created_at = datetime(2026, 5, 20, 1, 2, 3, tzinfo=timezone.utc)
    row = {
        "chunk_id": "chunk-scalars",
        "session_id": "sess-scalars",
        "chunk_index": 0,
        "text": "scalar-safe output",
        "created_at": created_at,
        "trace_id": UUID("12345678-1234-5678-1234-567812345678"),
    }

    class FakeBridge:
        def get_session_chunk(self, chunk_id: str, *, owner_id: str, before: int = 0, after: int = 0):
            assert chunk_id == "chunk-scalars"
            assert owner_id
            return row

    monkeypatch.setattr(session_cli, "get_session_memory_bridge", lambda: FakeBridge())

    assert session_cli.main(["expand-chunk", "chunk-scalars", "--owner", "owner-cli", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created_at"] == str(created_at)
    assert payload["trace_id"] == "12345678-1234-5678-1234-567812345678"


def test_quaid_session_expand_microchunk_json_breaks_result_cycles(monkeypatch, capsys) -> None:
    from core import session_cli

    result = {
        "microchunk": {"microchunk_id": "micro-cycle", "text": "micro text"},
        "pair": {"pair_id": "pair-cycle"},
        "window": [],
    }
    result["window"].append(result)

    class FakeBridge:
        def expand_microchunk(self, microchunk_id: str, *, owner_id: str, before: int = 0, after: int = 0):
            assert microchunk_id == "micro-cycle"
            assert owner_id
            return result

    monkeypatch.setattr(session_cli, "get_session_memory_bridge", lambda: FakeBridge())

    assert session_cli.main(["expand-microchunk", "micro-cycle", "--owner", "owner-cli", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["microchunk"]["microchunk_id"] == "micro-cycle"
    assert payload["window"] == ["[Circular]"]


def test_quaid_session_expand_chunk_not_found_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    quaid_home.mkdir(parents=True)
    memory_db = tmp_path / "memory.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(memory_db))

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "MEMORY_DB_PATH": str(memory_db),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [
            str(quaid_bin),
            "session",
            "expand-chunk",
            "missing-session-chunk",
            "--owner",
            "owner-cli",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "session chunk not found: missing-session-chunk" in result.stderr


def test_quaid_session_delegates_missing_command_to_session_cli(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    quaid_home.mkdir(parents=True)

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [str(quaid_bin), "session"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Quaid session inspection CLI" in result.stdout


def test_quaid_session_preserves_internal_plumbing_error(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    quaid_home.mkdir(parents=True)

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [str(quaid_bin), "session", "load-log"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "session log indexing/loading is internal runtime plumbing" in result.stderr


def test_quaid_datastore_lists_manifest_registry(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    quaid_bin = repo_root / "quaid"

    home = tmp_path / "home"
    quaid_home = home / ".quaid"
    quaid_home.mkdir(parents=True)

    env = {
        **os.environ,
        "HOME": str(home),
        "QUAID_HOME": str(quaid_home),
        "QUAID_PYTHON_BIN": os.environ.get("QUAID_PYTHON_BIN", "python3"),
    }
    result = subprocess.run(
        [str(quaid_bin), "datastore", "list", "--json"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert [item["id"] for item in payload["datastores"]] == ["docsdb", "insightdb", "memorydb", "sessiondb"]
