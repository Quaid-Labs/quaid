"""Shared fixtures for all test modules."""
import os
from pathlib import Path
import pytest

# Default workspace for tests: repository root, not a machine-specific path.
_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parents[3])

# Ensure a workspace hint exists before collection so module-level imports that
# resolve paths have a stable home directory context.
if not os.environ.get("CLAWDBOT_WORKSPACE"):
    os.environ["CLAWDBOT_WORKSPACE"] = _DEFAULT_WORKSPACE

from lib.adapter import reset_adapter


@pytest.fixture(autouse=True)
def _ensure_adapter_clean():
    """Reset adapter singleton before and after every test to prevent leaks."""
    reset_adapter()
    yield
    reset_adapter()


@pytest.fixture(autouse=True)
def _ensure_clawdbot_workspace(monkeypatch):
    """Ensure CLAWDBOT_WORKSPACE is set unless a test explicitly removes it."""
    if not os.environ.get("CLAWDBOT_WORKSPACE"):
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", _DEFAULT_WORKSPACE)


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
