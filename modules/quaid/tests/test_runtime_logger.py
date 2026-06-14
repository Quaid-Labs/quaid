import json


def test_runtime_logger_log_honors_quaid_now(tmp_path, monkeypatch):
    from lib.adapter import reset_adapter
    from core.runtime import logger as runtime_logger

    reset_adapter()
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-logger")
    monkeypatch.setenv("QUAID_NOW", "2026-03-18T05:06:07Z")

    runtime_logger.log("pytest", "event")

    log_dir = tmp_path / "instances" / "pytest-logger" / "logs"
    row = json.loads((log_dir / "pytest.log").read_text(encoding="utf-8"))
    assert row["ts"] == "2026-03-18T05:06:07Z"
    assert row["event"] == "event"


def test_runtime_logger_rotate_logs_uses_quaid_now_date(tmp_path, monkeypatch):
    from lib.adapter import reset_adapter
    from core.runtime import logger as runtime_logger

    reset_adapter()
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-logger")
    monkeypatch.setenv("QUAID_NOW", "2026-03-18T23:59:00Z")
    log_dir = tmp_path / "instances" / "pytest-logger" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "pytest.log").write_text("line\n", encoding="utf-8")

    runtime_logger.rotate_logs()

    archive = log_dir / "archive" / "pytest.2026-03-18.log"
    assert archive.read_text(encoding="utf-8") == "line\n"


def test_runtime_logger_clean_old_archives_uses_quaid_now(tmp_path, monkeypatch):
    from lib.adapter import reset_adapter
    from core.runtime import logger as runtime_logger

    reset_adapter()
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-logger")
    monkeypatch.setenv("QUAID_NOW", "2026-03-18T00:00:00Z")
    archive_dir = tmp_path / "instances" / "pytest-logger" / "logs" / "archive"
    archive_dir.mkdir(parents=True)
    old_archive = archive_dir / "pytest.2026-03-10.log"
    kept_archive = archive_dir / "pytest.2026-03-12.log"
    old_archive.write_text("old\n", encoding="utf-8")
    kept_archive.write_text("keep\n", encoding="utf-8")

    runtime_logger.clean_old_archives()

    assert not old_archive.exists()
    assert kept_archive.exists()
