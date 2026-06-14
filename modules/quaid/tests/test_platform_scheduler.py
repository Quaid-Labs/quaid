"""Unit tests for PlatformSchedulerServer, client, and shared project lock."""
import builtins
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch


def _short_tmp() -> Path:
    """Return a short-path temp directory suitable for Unix socket paths (max ~104 chars)."""
    d = Path(tempfile.mkdtemp(prefix="qps", dir="/tmp"))
    return d


def test_fail_hard_enabled_fails_closed_on_import_error(monkeypatch, caplog):
    from core import platform_scheduler

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    caplog.set_level("CRITICAL")

    assert platform_scheduler._fail_hard_enabled() is True
    assert "fail-hard policy unavailable in platform scheduler" in caplog.text


# ---- PlatformSchedulerServer ----

class TestPlatformSchedulerServer:
    @pytest.fixture(autouse=True)
    def _cleanup_servers(self):
        self._servers = []
        self._bases = []
        yield
        for server, thread in reversed(self._servers):
            server.stop()
            thread.join(timeout=2.0)
        for base in reversed(self._bases):
            shutil.rmtree(base, ignore_errors=True)

    def _start_server(self, base, slots=4):
        from core.platform_scheduler import PlatformSchedulerServer
        server = PlatformSchedulerServer(base, "tp", total_slots=slots)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        self._servers.append((server, t))
        self._bases.append(base)
        # Wait until the socket accepts connections. The AF_UNIX path appears
        # at bind() before listen(), so path existence alone is not readiness.
        sock_path = base / "shared" / "run" / "tp-scheduler.sock"
        for _ in range(50):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(str(sock_path))
                probe.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"scheduler socket did not become ready: {sock_path}")
        return server, sock_path

    def _client_sock(self, sock_path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock_path))
        return s

    def _send(self, s, msg):
        s.sendall((json.dumps(msg) + "\n").encode())
        buf = ""
        while "\n" not in buf:
            buf += s.recv(4096).decode()
        return json.loads(buf.split("\n")[0])

    def test_acquire_and_release(self):
        base = _short_tmp()
        server, sock_path = self._start_server(base, slots=2)
        c = self._client_sock(sock_path)
        resp = self._send(c, {"op": "acquire", "n": 1})
        assert resp["ok"] is True
        resp = self._send(c, {"op": "release", "n": 1})
        assert resp["ok"] is True
        c.close()

    def test_status_reflects_used_slots(self):
        base = _short_tmp()
        server, sock_path = self._start_server(base, slots=4)
        c = self._client_sock(sock_path)
        self._send(c, {"op": "acquire", "n": 2})
        st = self._send(c, {"op": "status"})
        assert st["slots_used"] == 2
        assert st["slots_total"] == 4
        c.close()

    def test_slots_reclaimed_on_disconnect(self):
        base = _short_tmp()
        server, sock_path = self._start_server(base, slots=2)
        c = self._client_sock(sock_path)
        self._send(c, {"op": "acquire", "n": 2})
        c.close()  # Disconnect without releasing
        time.sleep(0.2)
        # New client can acquire
        c2 = self._client_sock(sock_path)
        resp = self._send(c2, {"op": "acquire", "n": 2})
        assert resp["ok"] is True
        c2.close()

    def test_fifo_queue(self):
        """First waiter gets slot when one is released."""
        base = _short_tmp()
        server, sock_path = self._start_server(base, slots=1)
        c1 = self._client_sock(sock_path)
        c2 = self._client_sock(sock_path)
        # c1 fills the only slot
        self._send(c1, {"op": "acquire", "n": 1})
        # c2 queues an acquire in background
        result = []
        def _acquire():
            resp = self._send(c2, {"op": "acquire", "n": 1})
            result.append(resp)
        t = threading.Thread(target=_acquire, daemon=True)
        t.start()
        time.sleep(0.1)
        assert not result  # Still waiting
        # c1 releases — c2 should unblock
        self._send(c1, {"op": "release", "n": 1})
        t.join(timeout=2.0)
        assert result and result[0]["ok"] is True
        c1.close()
        c2.close()

    def test_queue_entry_dropped_on_disconnect(self):
        """Disconnecting while queued doesn't block other waiters."""
        base = _short_tmp()
        server, sock_path = self._start_server(base, slots=1)
        c1 = self._client_sock(sock_path)
        c2 = self._client_sock(sock_path)
        c3 = self._client_sock(sock_path)
        self._send(c1, {"op": "acquire", "n": 1})
        # c2 queues, then disconnects
        def _queue_then_disconnect():
            c2.sendall((json.dumps({"op": "acquire", "n": 1}) + "\n").encode())
            time.sleep(0.05)
            c2.close()
        threading.Thread(target=_queue_then_disconnect, daemon=True).start()
        time.sleep(0.1)
        # c3 queues
        result = []
        def _c3_acquire():
            result.append(self._send(c3, {"op": "acquire", "n": 1}))
        threading.Thread(target=_c3_acquire, daemon=True).start()
        time.sleep(0.1)
        # c1 releases — c3 should get slot (c2 dropped from queue)
        self._send(c1, {"op": "release", "n": 1})
        time.sleep(0.3)
        assert result and result[0]["ok"] is True
        c1.close()
        c3.close()

    def test_late_acquire_after_disconnect_does_not_leak_slots(self, tmp_path):
        """Acquire threads racing with disconnect must not grant dead clients."""
        from core.platform_scheduler import PlatformSchedulerServer, _Connection

        left, right = socket.socketpair()
        try:
            server = PlatformSchedulerServer(tmp_path, "tp", total_slots=1)
            conn = _Connection(left, None)

            server._handle_acquire(conn, 1)

            assert server._used == 0
            assert conn.held == 0
        finally:
            left.close()
            right.close()

    def test_late_queued_acquire_after_disconnect_returns_without_waiter_leak(self, tmp_path):
        """Dead clients must not be queued after disconnect cleanup has run."""
        from core.platform_scheduler import PlatformSchedulerServer, _Connection

        left, right = socket.socketpair()
        try:
            server = PlatformSchedulerServer(tmp_path, "tp", total_slots=1)
            server._used = 1
            conn = _Connection(left, None)

            thread = threading.Thread(target=server._handle_acquire, args=(conn, 1), daemon=True)
            thread.start()
            thread.join(timeout=0.5)

            assert not thread.is_alive()
            assert server._queue == []
            assert server._used == 1
            assert conn.held == 0
        finally:
            left.close()
            right.close()

    def test_registered_queued_acquire_is_woken_on_disconnect(self, tmp_path):
        """Queued acquire threads must exit when their registered client disconnects."""
        from core.platform_scheduler import PlatformSchedulerServer, _Connection

        left, right = socket.socketpair()
        try:
            server = PlatformSchedulerServer(tmp_path, "tp", total_slots=1)
            server._used = 1
            conn = _Connection(left, None)
            server._connections[id(left)] = conn

            thread = threading.Thread(target=server._handle_acquire, args=(conn, 1), daemon=True)
            thread.start()
            for _ in range(50):
                if server._queue:
                    break
                time.sleep(0.01)
            assert len(server._queue) == 1

            server._on_disconnect(conn)
            thread.join(timeout=0.5)

            assert not thread.is_alive()
            assert server._queue == []
            assert server._used == 1
            assert conn.held == 0
        finally:
            left.close()
            right.close()

    def test_client_release_unblocks_while_same_client_acquire_waits(self):
        """A blocked acquire must not hold the lock needed by release."""
        from core.platform_scheduler import PlatformSchedulerClient

        base = _short_tmp()
        self._start_server(base, slots=1)
        client = PlatformSchedulerClient(base, "tp")
        client.acquire(1)
        acquired = []
        released = []

        def _second_acquire():
            client.acquire(1)
            acquired.append(True)

        def _release_first():
            client.release(1)
            released.append(True)

        acquire_thread = threading.Thread(target=_second_acquire, daemon=True)
        acquire_thread.start()
        time.sleep(0.1)
        release_thread = threading.Thread(target=_release_first, daemon=True)
        release_thread.start()

        release_thread.join(timeout=1.0)
        acquire_thread.join(timeout=1.0)
        client.close()

        assert released == [True]
        assert acquired == [True]

    def test_ensure_scheduler_alive_raises_start_failure_when_fail_hard(self, monkeypatch):
        from core import platform_scheduler

        base = _short_tmp()
        self._bases.append(base)
        monkeypatch.setattr(platform_scheduler, "_read_pid", lambda *_args: None)
        monkeypatch.setattr(platform_scheduler, "start_scheduler", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")))
        monkeypatch.setattr(platform_scheduler, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="spawn failed"):
            platform_scheduler.ensure_scheduler_alive(base, "tp")

    def test_ensure_scheduler_alive_handles_lock_open_failure_when_fail_open(self, monkeypatch):
        from core import platform_scheduler
        import builtins

        base = _short_tmp()
        self._bases.append(base)
        monkeypatch.setattr(platform_scheduler, "_read_pid", lambda *_args: None)
        monkeypatch.setattr(platform_scheduler, "_fail_hard_enabled", lambda: False)

        real_open = builtins.open

        def _open(path, *args, **kwargs):
            if str(path).endswith("tp-scheduler-start.lock"):
                raise OSError("lock open failed")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _open)

        assert platform_scheduler.ensure_scheduler_alive(base, "tp") == -1

    def test_start_scheduler_child_crash_uses_nonzero_exit_source_guard(self):
        source = Path(__file__).resolve().parents[1] / "core" / "platform_scheduler.py"
        text = source.read_text(encoding="utf-8")
        crash_handler = text[text.index("logger.error(\"platform scheduler crashed"): text.index("return pid", text.index("logger.error(\"platform scheduler crashed"))]

        assert "os._exit(1)" in crash_handler


# ---- Shared project lock ----

class TestSharedProjectLock:
    def test_first_caller_gets_lock(self, tmp_path):
        from lib.shared_project_lock import try_claim_project_update
        with try_claim_project_update(tmp_path, "myproject") as claimed:
            assert claimed is True

    def test_second_caller_skips_if_checkpoint_fresh(self, tmp_path):
        from lib.shared_project_lock import try_claim_project_update, write_checkpoint
        write_checkpoint(tmp_path, "myproject")
        with try_claim_project_update(tmp_path, "myproject", max_age_seconds=3600) as claimed:
            assert claimed is False

    def test_second_caller_proceeds_if_checkpoint_stale(self, tmp_path):
        from lib.shared_project_lock import try_claim_project_update, write_checkpoint, _checkpoint_path
        import time
        write_checkpoint(tmp_path, "myproject")
        # Make checkpoint look old
        cp = _checkpoint_path(tmp_path, "myproject")
        cp.write_text(str(time.time() - 7200))
        with try_claim_project_update(tmp_path, "myproject", max_age_seconds=3600) as claimed:
            assert claimed is True

    def test_concurrent_callers_only_one_proceeds(self, tmp_path):
        from lib.shared_project_lock import try_claim_project_update
        results = []
        barrier = threading.Barrier(2)

        def _worker():
            barrier.wait()
            with try_claim_project_update(tmp_path, "shared-proj", max_age_seconds=1) as claimed:
                if claimed:
                    time.sleep(0.1)  # Simulate work
                results.append(claimed)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        # Exactly one should have proceeded
        assert results.count(True) == 1
        assert results.count(False) == 1

    def test_write_checkpoint_updates_age(self, tmp_path):
        from lib.shared_project_lock import write_checkpoint, _read_checkpoint_age
        write_checkpoint(tmp_path, "proj")
        age = _read_checkpoint_age(tmp_path, "proj")
        assert age is not None
        assert age < 2.0
