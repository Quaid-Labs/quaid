"""Unit tests for adaptors/factory.py.

Covers create_adapter() — known-type normalization and unknown-type errors.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adaptors.factory import create_adapter


def test_create_adapter_error_guides_standalone_resolution():
    with pytest.raises(RuntimeError, match="standalone.*lib\\.adapter\\.get_adapter"):
        create_adapter("standalone")


def test_create_adapter_unknown_kind_raises():
    with pytest.raises(RuntimeError, match="Unsupported adapter type"):
        create_adapter("unknown-adapter")


def test_create_adapter_empty_string_raises():
    with pytest.raises(RuntimeError):
        create_adapter("")


def test_create_adapter_none_raises():
    with pytest.raises(RuntimeError):
        create_adapter(None)


def test_create_adapter_openclaw_imports_openclaw_adapter():
    mock_adapter = MagicMock()
    with patch.dict("sys.modules", {
        "adaptors.openclaw.adapter": MagicMock(OpenClawAdapter=MagicMock(return_value=mock_adapter))
    }):
        result = create_adapter("openclaw")
        assert result is mock_adapter


def test_create_adapter_openclaw_case_insensitive():
    mock_adapter = MagicMock()
    with patch.dict("sys.modules", {
        "adaptors.openclaw.adapter": MagicMock(OpenClawAdapter=MagicMock(return_value=mock_adapter))
    }):
        result = create_adapter("OPENCLAW")
        assert result is mock_adapter


def test_create_adapter_claude_code_with_hyphen():
    mock_adapter = MagicMock()
    with patch.dict("sys.modules", {
        "adaptors.claude_code.adapter": MagicMock(ClaudeCodeAdapter=MagicMock(return_value=mock_adapter))
    }):
        result = create_adapter("claude-code")
        assert result is mock_adapter


def test_create_adapter_claude_code_with_underscore():
    mock_adapter = MagicMock()
    with patch.dict("sys.modules", {
        "adaptors.claude_code.adapter": MagicMock(ClaudeCodeAdapter=MagicMock(return_value=mock_adapter))
    }):
        result = create_adapter("claude_code")
        assert result is mock_adapter


def test_create_adapter_claudecode_alias():
    mock_adapter = MagicMock()
    with patch.dict("sys.modules", {
        "adaptors.claude_code.adapter": MagicMock(ClaudeCodeAdapter=MagicMock(return_value=mock_adapter))
    }):
        result = create_adapter("claudecode")
        assert result is mock_adapter


def test_create_adapter_codex():
    mock_adapter = MagicMock()
    with patch.dict("sys.modules", {
        "adaptors.codex.adapter": MagicMock(CodexAdapter=MagicMock(return_value=mock_adapter))
    }):
        result = create_adapter("codex")
        assert result is mock_adapter
