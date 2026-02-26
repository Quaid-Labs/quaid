"""Adapter contract conformance tests.

These tests define the minimum behavior every adapter must satisfy.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.adapter import StandaloneAdapter, ChannelInfo
from adaptors.openclaw.adapter import OpenClawAdapter


def _assert_common_contract(adapter):
    # Required path surface
    assert isinstance(adapter.quaid_home(), Path)
    assert isinstance(adapter.data_dir(), Path)
    assert isinstance(adapter.config_dir(), Path)
    assert isinstance(adapter.logs_dir(), Path)
    assert isinstance(adapter.journal_dir(), Path)
    assert isinstance(adapter.projects_dir(), Path)

    # Transcript helpers are required by extract/docs flows
    transcript = adapter.build_transcript([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ])
    assert "User: hello" in transcript
    assert "Assistant: world" in transcript


def test_standalone_contract(tmp_path):
    adapter = StandaloneAdapter(home=tmp_path)
    _assert_common_contract(adapter)

    # Standalone should not filter host noise by default.
    txt = adapter.build_transcript([
        {"role": "user", "content": "GatewayRestart: reconnecting"},
    ])
    assert "GatewayRestart" in txt

    # Notifications always succeed (stderr fallback)
    assert adapter.notify("contract test", dry_run=True) is True


@pytest.mark.adapter_openclaw
def test_openclaw_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
    adapter = OpenClawAdapter()
    _assert_common_contract(adapter)

    # OpenClaw adapter-specific filtering should remove gateway noise.
    txt = adapter.build_transcript([
        {"role": "user", "content": "GatewayRestart: reconnecting"},
        {"role": "assistant", "content": "ok"},
    ])
    assert "GatewayRestart" not in txt
    assert "Assistant: ok" in txt


@pytest.mark.adapter_openclaw
def test_openclaw_channel_info_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(json.dumps({
        "agent:main:main": {
            "lastChannel": "telegram",
            "lastTo": "123",
            "lastAccountId": "default",
        }
    }))

    adapter = OpenClawAdapter()
    adapter._find_sessions_json = lambda: sessions_file
    info = adapter.get_last_channel()

    assert isinstance(info, ChannelInfo)
    assert info.channel == "telegram"
    assert info.target == "123"
