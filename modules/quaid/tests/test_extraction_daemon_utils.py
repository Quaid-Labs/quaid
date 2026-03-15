"""Unit tests for extraction_daemon utility functions.

Covers: _validate_session_id, write_signal/read_pending_signals/
mark_signal_processed, read_cursor/write_cursor, read_transcript_slice,
count_transcript_lines, read_carryover/write_carryover/clear_carryover.

All tests use QUAID_HOME/QUAID_INSTANCE isolation via monkeypatch.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.extraction_daemon as daemon


@pytest.fixture(autouse=True)
def isolated_daemon(tmp_path, monkeypatch):
    """Redirect all daemon paths to a temporary directory."""
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-instance")
    yield tmp_path


# ---------------------------------------------------------------------------
# _validate_session_id
# ---------------------------------------------------------------------------


class TestValidateSessionId:
    def test_valid_alphanumeric(self):
        assert daemon._validate_session_id("abc123") == "abc123"

    def test_valid_with_hyphens_and_underscores(self):
        sid = "sess-abc_123"
        assert daemon._validate_session_id(sid) == sid

    def test_max_length_valid(self):
        sid = "a" * 128
        assert daemon._validate_session_id(sid) == sid

    def test_too_long_gets_fallback(self):
        sid = "a" * 129
        result = daemon._validate_session_id(sid)
        assert result != sid
        assert result.startswith("unknown-")

    def test_empty_string_gets_fallback(self):
        result = daemon._validate_session_id("")
        assert result.startswith("unknown-")

    def test_path_traversal_gets_fallback(self):
        result = daemon._validate_session_id("../../etc/passwd")
        assert result.startswith("unknown-")

    def test_space_in_id_gets_fallback(self):
        result = daemon._validate_session_id("bad id")
        assert result.startswith("unknown-")


# ---------------------------------------------------------------------------
# write_signal / read_pending_signals / mark_signal_processed
# ---------------------------------------------------------------------------


class TestSignalCycle:
    def test_write_signal_creates_file(self):
        path = daemon.write_signal(
            signal_type="session_end",
            session_id="sess-1",
            transcript_path="/tmp/sess.jsonl",
        )
        assert path.exists()

    def test_write_signal_content(self):
        path = daemon.write_signal(
            signal_type="session_end",
            session_id="sess-1",
            transcript_path="/tmp/sess.jsonl",
            adapter="claude_code",
        )
        data = json.loads(path.read_text())
        assert data["type"] == "session_end"
        assert data["session_id"] == "sess-1"
        assert data["adapter"] == "claude_code"

    def test_unknown_signal_type_defaults_to_session_end(self):
        path = daemon.write_signal(
            signal_type="bogus_type",
            session_id="sess-1",
            transcript_path="/tmp/sess.jsonl",
        )
        data = json.loads(path.read_text())
        assert data["type"] == "session_end"

    def test_valid_signal_types_accepted(self):
        for sig_type in ("compaction", "reset", "session_end", "timeout"):
            path = daemon.write_signal(
                signal_type=sig_type,
                session_id="sess-valid",
                transcript_path="/tmp/x.jsonl",
            )
            data = json.loads(path.read_text())
            assert data["type"] == sig_type

    def test_read_pending_signals_empty_when_no_dir(self, tmp_path, monkeypatch):
        # Point signal dir to nonexistent path
        monkeypatch.setattr(daemon, "_signal_dir", lambda: tmp_path / "nonexistent")
        assert daemon.read_pending_signals() == []

    def test_read_pending_signals_returns_written_signal(self):
        daemon.write_signal(
            signal_type="session_end",
            session_id="sess-1",
            transcript_path="/tmp/x.jsonl",
        )
        signals = daemon.read_pending_signals()
        assert len(signals) == 1
        assert signals[0]["session_id"] == "sess-1"
        assert "_signal_path" in signals[0]

    def test_read_pending_signals_skips_malformed_json(self, tmp_path, monkeypatch):
        sig_dir = tmp_path / "data" / "extraction-signals"
        sig_dir.mkdir(parents=True)
        (sig_dir / "bad.json").write_text("}{not json")
        monkeypatch.setattr(daemon, "_signal_dir", lambda: sig_dir)
        signals = daemon.read_pending_signals()
        assert signals == []

    def test_mark_signal_processed_removes_file(self):
        path = daemon.write_signal(
            signal_type="session_end",
            session_id="sess-1",
            transcript_path="/tmp/x.jsonl",
        )
        assert path.exists()
        daemon.mark_signal_processed({"_signal_path": str(path)})
        assert not path.exists()

    def test_mark_signal_processed_no_path_is_noop(self):
        daemon.mark_signal_processed({})  # Should not raise

    def test_mark_signal_processed_refuses_path_outside_signal_dir(self, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        daemon.mark_signal_processed({"_signal_path": str(outside)})
        # File should still exist (refused to delete)
        assert outside.exists()


# ---------------------------------------------------------------------------
# read_cursor / write_cursor
# ---------------------------------------------------------------------------


class TestCursor:
    def test_read_cursor_nonexistent_returns_defaults(self):
        cursor = daemon.read_cursor("new-session")
        assert cursor["line_offset"] == 0
        assert cursor["transcript_path"] == ""

    def test_write_then_read_cursor(self):
        daemon.write_cursor("sess-1", line_offset=42, transcript_path="/tmp/t.jsonl")
        cursor = daemon.read_cursor("sess-1")
        assert cursor["line_offset"] == 42
        assert cursor["transcript_path"] == "/tmp/t.jsonl"

    def test_write_cursor_updates_existing(self):
        daemon.write_cursor("sess-1", 10, "/tmp/a.jsonl")
        daemon.write_cursor("sess-1", 25, "/tmp/a.jsonl")
        cursor = daemon.read_cursor("sess-1")
        assert cursor["line_offset"] == 25

    def test_read_cursor_malformed_json_returns_defaults(self, tmp_path, monkeypatch):
        cursor_dir = tmp_path / "test-instance" / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "bad-sess.json").write_text("}{not json")
        cursor = daemon.read_cursor("bad-sess")
        assert cursor["line_offset"] == 0


# ---------------------------------------------------------------------------
# read_transcript_slice / count_transcript_lines
# ---------------------------------------------------------------------------


class TestTranscriptUtils:
    def test_count_transcript_lines_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert daemon.count_transcript_lines(str(f)) == 0

    def test_count_transcript_lines_correct(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("line1\nline2\nline3\n")
        assert daemon.count_transcript_lines(str(f)) == 3

    def test_count_transcript_lines_nonexistent_returns_zero(self, tmp_path):
        assert daemon.count_transcript_lines(str(tmp_path / "missing.jsonl")) == 0

    def test_read_transcript_slice_from_start(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("line0\nline1\nline2\n")
        lines = daemon.read_transcript_slice(str(f), from_line=0)
        assert lines == ["line0\n", "line1\n", "line2\n"]

    def test_read_transcript_slice_with_offset(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("line0\nline1\nline2\n")
        lines = daemon.read_transcript_slice(str(f), from_line=2)
        assert lines == ["line2\n"]

    def test_read_transcript_slice_nonexistent_returns_empty(self, tmp_path):
        lines = daemon.read_transcript_slice(str(tmp_path / "missing.jsonl"), from_line=0)
        assert lines == []

    def test_read_transcript_slice_offset_past_end_returns_empty(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text("line0\nline1\n")
        lines = daemon.read_transcript_slice(str(f), from_line=99)
        assert lines == []


# ---------------------------------------------------------------------------
# read_carryover / write_carryover / clear_carryover
# ---------------------------------------------------------------------------


