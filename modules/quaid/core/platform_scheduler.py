"""Platform-level LLM slot scheduler.

One coordinator process per platform (e.g. 'claude-code') per QUAID_HOME.
All instances on the same platform share a bounded slot pool, preventing
N×max_workers concurrent LLM calls against the same API credentials.

Socket: QUAID_HOME/shared/run/<platform>-scheduler.sock
PID:    QUAID_HOME/shared/run/<platform>-scheduler.pid

Protocol (newline-delimited JSON):
  Client → Server:  {"op": "acquire", "n": 1}
                    {"op": "release", "n": 1}
                    {"op": "status"}
  Server → Client:  {"ok": true}          (after slot granted)
                    {"ok": true, "slots_total": N, "slots_used": M, "queue_depth": K}
                    {"error": "..."}

On client disconnect: held slots are reclaimed, queued requests dropped.
"""

import json
import logging
import os
import select
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("quaid.platform_scheduler")

_DEFAULT_SLOTS = 8


def _shared_run_dir(quaid_home: Path) -> Path:
    d = quaid_home / "shared" / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sock_path(quaid_home: Path, platform: str) -> Path:
    return _shared_run_dir(quaid_home) / f"{platform}-scheduler.sock"


def _pid_path(quaid_home: Path, platform: str) -> Path:
    return _shared_run_dir(quaid_home) / f"{platform}-scheduler.pid"


# ---- Server ----------------------------------------------------------------

class _Connection:
    """Tracks state for one connected client."""
    def __init__(self, conn: socket.socket, addr):
        self.conn = conn
        self.addr = addr
        self.held: int = 0          # slots currently held by this connection
        self.buf: str = ""          # incomplete line buffer


class PlatformSchedulerServer:
    """Slot-pool server. Run in a dedicated process via start_scheduler()."""

    def __init__(self, quaid_home: Path, platform: str, total_slots: int = _DEFAULT_SLOTS):
        self._home = quaid_home
        self._platform = platform
        self._total = total_slots
        self._used = 0
        self._lock = threading.Lock()
        # FIFO queue: list of (conn_id, n_requested, event)
        self._queue: List[Tuple[int, int, threading.Event]] = []
        self._connections: Dict[int, _Connection] = {}  # id(conn.conn) → _Connection
        self._server_sock: Optional[socket.socket] = None
        self._running = False

    def _available(self) -> int:
        return self._total - self._used

    def _try_flush_queue(self) -> None:
        """Grant waiting requests while slots are available. Must hold self._lock."""
        while self._queue and self._available() > 0:
            conn_id, n, event = self._queue[0]
            if conn_id not in self._connections:
                # Client disconnected while waiting — drop it
                self._queue.pop(0)
                continue
            if self._available() >= n:
                self._queue.pop(0)
                self._used += n
                self._connections[conn_id].held += n
                event.set()
            else:
                break  # Not enough slots; FIFO — don't skip ahead

    def _handle_acquire(self, conn_obj: _Connection, n: int) -> None:
        n = max(1, int(n))
        event = threading.Event()
        conn_id = id(conn_obj.conn)
        with self._lock:
            if self._available() >= n:
                self._used += n
                conn_obj.held += n
                event.set()
            else:
                self._queue.append((conn_id, n, event))
        # Block until granted (or connection dies — handled by disconnect cleanup)
        event.wait()
        # Check if we were disconnected during wait
        with self._lock:
            if conn_id not in self._connections:
                # Slots were reclaimed on disconnect — nothing to send
                return
        try:
            conn_obj.conn.sendall(b'{"ok": true}\n')
        except OSError:
            pass

    def _handle_release(self, conn_obj: _Connection, n: int) -> None:
        n = max(1, int(n))
        with self._lock:
            actual = min(n, conn_obj.held)
            conn_obj.held -= actual
            self._used -= actual
            self._try_flush_queue()
        try:
            conn_obj.conn.sendall(b'{"ok": true}\n')
        except OSError:
            pass

    def _handle_status(self, conn_obj: _Connection) -> None:
        with self._lock:
            resp = json.dumps({
                "ok": True,
                "slots_total": self._total,
                "slots_used": self._used,
                "queue_depth": len(self._queue),
            })
        try:
            conn_obj.conn.sendall((resp + "\n").encode())
        except OSError:
            pass

    def _on_disconnect(self, conn_obj: _Connection) -> None:
        conn_id = id(conn_obj.conn)
        with self._lock:
            self._connections.pop(conn_id, None)
            # Remove queued requests from this connection
            self._queue = [(cid, n, ev) for cid, n, ev in self._queue if cid != conn_id]
            # Reclaim held slots
            self._used -= conn_obj.held
            self._used = max(0, self._used)
            conn_obj.held = 0
            self._try_flush_queue()
        try:
            conn_obj.conn.close()
        except OSError:
            pass

    def _handle_client(self, conn_obj: _Connection) -> None:
        """Read messages from one client connection in a dedicated thread."""
        try:
            while self._running:
                try:
                    data = conn_obj.conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                conn_obj.buf += data.decode("utf-8", errors="replace")
                while "\n" in conn_obj.buf:
                    line, conn_obj.buf = conn_obj.buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    op = msg.get("op", "")
                    if op == "acquire":
                        # Run blocking wait in a thread so we don't block the reader loop
                        threading.Thread(
                            target=self._handle_acquire,
                            args=(conn_obj, msg.get("n", 1)),
                            daemon=True,
                        ).start()
                    elif op == "release":
                        self._handle_release(conn_obj, msg.get("n", 1))
                    elif op == "status":
                        self._handle_status(conn_obj)
        finally:
            self._on_disconnect(conn_obj)

    def run(self) -> None:
        sock_path = _sock_path(self._home, self._platform)
        pid_path = _pid_path(self._home, self._platform)

        # Clean up stale socket
        sock_path.unlink(missing_ok=True)

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(sock_path))
        self._server_sock.listen(32)
        self._running = True

        pid_path.write_text(str(os.getpid()) + "\n")

        logger.info(
            "platform scheduler started platform=%s slots=%d pid=%d sock=%s",
            self._platform, self._total, os.getpid(), sock_path,
        )

        def _shutdown(sig, frame):
            self._running = False
            try:
                self._server_sock.close()
            except OSError:
                pass

        # Signal handlers can only be set from the main thread; skip in non-main threads
        # (e.g. during tests). In production the server runs as a dedicated forked process
        # where run() is always called from the main thread.
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)

        try:
            while self._running:
                try:
                    ready, _, _ = select.select([self._server_sock], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    conn, addr = self._server_sock.accept()
                except OSError:
                    break
                conn_obj = _Connection(conn, addr)
                with self._lock:
                    self._connections[id(conn)] = conn_obj
                threading.Thread(
                    target=self._handle_client,
                    args=(conn_obj,),
                    daemon=True,
                ).start()
        finally:
            self._running = False
            pid_path.unlink(missing_ok=True)
            sock_path.unlink(missing_ok=True)
            logger.info("platform scheduler stopped platform=%s", self._platform)


# ---- Client ----------------------------------------------------------------

class PlatformSchedulerClient:
    """Client that acquires/releases slots from the platform scheduler.

    Used as a context manager:
        with PlatformSchedulerClient(home, platform).slot():
            ... LLM work ...
    """

    def __init__(self, quaid_home: Path, platform: str):
        self._home = quaid_home
        self._platform = platform
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(_sock_path(self._home, self._platform)))
        return sock

    def _send(self, sock: socket.socket, msg: dict) -> dict:
        sock.sendall((json.dumps(msg) + "\n").encode())
        buf = ""
        while "\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("scheduler connection closed")
            buf += chunk.decode("utf-8", errors="replace")
        line = buf.split("\n")[0]
        return json.loads(line)

    def acquire(self, n: int = 1) -> None:
        with self._lock:
            if self._sock is None:
                self._sock = self._connect()
            self._send(self._sock, {"op": "acquire", "n": n})

    def release(self, n: int = 1) -> None:
        with self._lock:
            if self._sock is None:
                return
            try:
                self._send(self._sock, {"op": "release", "n": n})
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    class _SlotContext:
        def __init__(self, client: "PlatformSchedulerClient", n: int):
            self._client = client
            self._n = n
        def __enter__(self):
            self._client.acquire(self._n)
            return self
        def __exit__(self, *_):
            self._client.release(self._n)

    def slot(self, n: int = 1) -> "_SlotContext":
        return self._SlotContext(self, n)

    def status(self) -> dict:
        with self._lock:
            if self._sock is None:
                self._sock = self._connect()
            return self._send(self._sock, {"op": "status"})


# ---- Lifecycle -------------------------------------------------------------

def _read_pid(quaid_home: Path, platform: str) -> Optional[int]:
    p = _pid_path(quaid_home, platform)
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        p.unlink(missing_ok=True)
        return None


def start_scheduler(quaid_home: Path, platform: str, total_slots: int = _DEFAULT_SLOTS) -> int:
    """Fork a scheduler process. Returns child PID."""
    pid = os.fork()
    if pid == 0:
        # Child: become the scheduler
        os.setsid()
        # Redirect stdio
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        try:
            server = PlatformSchedulerServer(quaid_home, platform, total_slots)
            server.run()
        except Exception as e:
            logger.error("platform scheduler crashed: %s", e)
        os._exit(0)
    return pid


def ensure_scheduler_alive(quaid_home: Path, platform: str, total_slots: int = _DEFAULT_SLOTS) -> int:
    """Ensure a platform scheduler is running. Start if not. Returns PID."""
    pid = _read_pid(quaid_home, platform)
    if pid is not None:
        return pid
    # Use a lock file to avoid two instances racing to start the scheduler
    lock_path = _shared_run_dir(quaid_home) / f"{platform}-scheduler-start.lock"
    import fcntl
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Double-check after acquiring lock
        pid = _read_pid(quaid_home, platform)
        if pid is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
            return pid
        child_pid = start_scheduler(quaid_home, platform, total_slots)
        # Brief wait for socket to appear
        sock_path = _sock_path(quaid_home, platform)
        for _ in range(20):
            if sock_path.exists():
                break
            time.sleep(0.1)
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        lock_path.unlink(missing_ok=True)
        return child_pid
    except Exception as e:
        logger.warning("ensure_scheduler_alive failed: %s", e)
        try:
            fd.close()
        except Exception:
            pass
        return -1


def get_platform_scheduler_client(quaid_home: Path, platform: str, total_slots: int = _DEFAULT_SLOTS) -> Optional[PlatformSchedulerClient]:
    """Get a connected client, starting the scheduler if needed.

    Returns None if the scheduler is unavailable (non-fatal: callers
    should proceed without slot gating rather than failing).
    """
    try:
        ensure_scheduler_alive(quaid_home, platform, total_slots)
        client = PlatformSchedulerClient(quaid_home, platform)
        # Test connection
        client.status()
        return client
    except Exception as e:
        logger.warning("platform scheduler unavailable (%s): proceeding without slot gating", e)
        return None
