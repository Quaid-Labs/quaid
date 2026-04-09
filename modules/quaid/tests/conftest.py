"""Shared fixtures for all test modules."""
import os
from pathlib import Path
import pytest

# Default workspace for tests: repository root, not a machine-specific path.
_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parents[3])
_DEFAULT_TEST_HOME = Path(os.environ.get("QUAID_HOME", str(Path(_DEFAULT_WORKSPACE) / ".pytest-home")))
_DEFAULT_VISIBLE_HOME = Path(
    os.environ.get(
        "QUAID_VISIBLE_HOME",
        str(
            _DEFAULT_TEST_HOME.with_name(_DEFAULT_TEST_HOME.name[1:])
            if _DEFAULT_TEST_HOME.name.startswith(".") and len(_DEFAULT_TEST_HOME.name) > 1
            else _DEFAULT_TEST_HOME
        ),
    )
)
_DEFAULT_INSTANCE = "pytest-runner"

# Seed an explicit adapter config before collection so import-time adapter
# resolution never relies on hidden fallbacks.
if not os.environ.get("QUAID_HOME"):
    os.environ["QUAID_HOME"] = str(_DEFAULT_TEST_HOME)
if not os.environ.get("QUAID_VISIBLE_HOME"):
    os.environ["QUAID_VISIBLE_HOME"] = str(_DEFAULT_VISIBLE_HOME)
if not os.environ.get("QUAID_INSTANCE"):
    os.environ["QUAID_INSTANCE"] = _DEFAULT_INSTANCE

# Instance-aware config: QUAID_HOME/instances/<instance>/memory.json
_instance_cfg = _DEFAULT_TEST_HOME / "instances" / _DEFAULT_INSTANCE / "memory.json"
_instance_cfg.parent.mkdir(parents=True, exist_ok=True)
if not _instance_cfg.exists():
    _instance_cfg.write_text('{"adapter":{"type":"standalone"}}', encoding="utf-8")

# Legacy flat config (backward compat during transition)
_adapter_cfg = _DEFAULT_TEST_HOME / "config" / "memory.json"
_adapter_cfg.parent.mkdir(parents=True, exist_ok=True)
if not _adapter_cfg.exists():
    _adapter_cfg.write_text('{"adapter":{"type":"standalone"}}', encoding="utf-8")

(_DEFAULT_VISIBLE_HOME / "instances" / _DEFAULT_INSTANCE / "journal").mkdir(parents=True, exist_ok=True)

# Ensure a workspace hint exists before collection so module-level imports that
# resolve paths have a stable home directory context.
if not os.environ.get("OPENCLAW_WORKSPACE"):
    os.environ["OPENCLAW_WORKSPACE"] = _DEFAULT_WORKSPACE

from lib.adapter import reset_adapter


@pytest.fixture(autouse=True)
def _ensure_adapter_clean():
    """Reset adapter singleton before and after every test to prevent leaks."""
    reset_adapter()
    yield
    reset_adapter()


@pytest.fixture(autouse=True)
def _ensure_instance_env(monkeypatch):
    """Ensure QUAID_INSTANCE is set unless a test explicitly removes it."""
    if not os.environ.get("QUAID_INSTANCE"):
        monkeypatch.setenv("QUAID_INSTANCE", _DEFAULT_INSTANCE)

@pytest.fixture(autouse=True)
def _ensure_openclaw_workspace(monkeypatch):
    """Ensure OPENCLAW_WORKSPACE is set unless a test explicitly removes it."""
    if not os.environ.get("OPENCLAW_WORKSPACE"):
        monkeypatch.setenv("OPENCLAW_WORKSPACE", _DEFAULT_WORKSPACE)


@pytest.fixture
def test_adapter(tmp_path):
    """Provide a TestAdapter with canned LLM responses and call recording.

    Usage::

        def test_something(test_adapter):
            # ... code under test ...
            assert len(test_adapter.llm_calls) == 1
    """
    from lib.adapter import TestAdapter, set_adapter
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    return adapter
