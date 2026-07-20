import importlib.util
from pathlib import Path


def _load_cleanup_module():
    script = Path(__file__).parent / "livetest" / "scripts" / "cleanup-m7-canary.py"
    spec = importlib.util.spec_from_file_location("cleanup_m7_canary", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cleanup_m7_pending_signal_files_ignore_dot_markers(tmp_path):
    cleanup = _load_cleanup_module()
    signal_dir = tmp_path / "extraction-signals"
    signal_dir.mkdir()
    (signal_dir / ".last_reset_signal").write_text("marker\n", encoding="utf-8")
    (signal_dir / ".last_reset_signal.openclaw").write_text("marker\n", encoding="utf-8")
    (signal_dir / ".locks").mkdir()
    real_signal = signal_dir / "session_end-sess-1.json"
    real_signal.write_text("{}\n", encoding="utf-8")

    assert cleanup._pending_signal_files(tmp_path) == [real_signal]


def test_cleanup_m7_pending_signal_files_clean_with_only_dot_markers(tmp_path):
    cleanup = _load_cleanup_module()
    signal_dir = tmp_path / "extraction-signals"
    signal_dir.mkdir()
    (signal_dir / ".last_reset_signal").write_text("marker\n", encoding="utf-8")
    (signal_dir / ".last_reset_signal.openclaw").write_text("marker\n", encoding="utf-8")

    assert cleanup._pending_signal_files(tmp_path) == []
