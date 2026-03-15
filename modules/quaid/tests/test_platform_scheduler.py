"""Unit tests for PlatformSchedulerServer, client, and shared project lock."""
import json
import os
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


# ---- PlatformSchedulerServer ----

class TestPlatformSchedulerServer:
    def _start_server(self, base, slots=4):
        from core.platform_scheduler import PlatformSchedulerServer
        server = PlatformSchedulerServer(base, "tp", total_slots=slots)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        # Wait for socket
        sock_path = base / "shared" / "run" / "tp-scheduler.sock"
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)
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
