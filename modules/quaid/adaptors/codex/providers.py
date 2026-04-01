"""LLM provider implementations for the Codex adapter."""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from lib.providers import LLMProvider, LLMResult

logger = logging.getLogger(__name__)


def _coerce_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            parts.append(_coerce_text(item))
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            if key in value:
                text = _coerce_text(value.get(key))
                if text:
                    return text
    return ""


def _extract_agent_text(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    item_type = str(item.get("type") or "").strip()
    if item_type == "agentMessage":
        text = _coerce_text(item.get("text") or item.get("message") or item.get("content"))
        if text:
            return text
    if item_type == "message":
        role = str(item.get("role") or "").strip().lower()
        if role == "assistant":
            text = _coerce_text(item.get("content") or item.get("text") or item.get("message"))
            if text:
                return text
    for key in ("content", "text", "message", "item"):
        if key not in item:
            continue
        text = _coerce_text(item.get(key))
        if text:
            return text
    return ""


class _CodexAppServerManager:
    """Long-lived stdio bridge to `codex app-server`."""

    def __init__(self, binary: str = ""):
        self._binary = binary.strip()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._listeners: List["queue.Queue[dict]"] = []
        self._initialized = False

    @staticmethod
    def _resolve_binary() -> str:
        explicit = str(os.environ.get("QUAID_CODEX_BIN", "") or "").strip()
        if explicit:
            return explicit
        for candidate in (
            shutil.which("codex"),
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        ):
            if candidate and Path(candidate).exists():
                return candidate
        raise RuntimeError("Could not locate `codex` binary for Codex app-server")

    def _proc_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start_locked(self) -> None:
        if self._proc_alive():
            return
        binary = self._binary or self._resolve_binary()
        proc = subprocess.Popen(
            [binary, "app-server", "--disable", "codex_hooks", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "NO_COLOR": "1"},
        )
        self._proc = proc
        self._initialized = False
        self._reader = threading.Thread(target=self._read_loop, name="quaid-codex-app-server", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._stderr_loop, name="quaid-codex-app-server-stderr", daemon=True)
        self._stderr_reader.start()
        self._initialize_locked()

    def ensure_running(self) -> None:
        with self._lock:
            self._start_locked()

    def _initialize_locked(self) -> None:
        if self._initialized:
            return
        result = self._request_locked(
            "initialize",
            {
                "clientInfo": {"name": "quaid", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=30.0,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Codex app-server initialize returned an invalid payload")
        self._initialized = True

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._initialized = False
            if proc is None:
                return
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            logger.debug("Codex app-server stderr: %s", line.rstrip())

    def _broadcast(self, payload: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(payload)
            except Exception:
                continue

    def _fail_pending(self, message: str) -> None:
        error_payload = {"error": {"message": message}}
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            try:
                waiter.put_nowait(error_payload)
            except Exception:
                pass
        self._broadcast({"method": "__quaid/process_closed__", "params": {"message": message}})

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Codex app-server emitted non-JSON line: %s", line)
                continue
            if isinstance(payload, dict) and "id" in payload:
                with self._pending_lock:
                    waiter = self._pending.pop(int(payload["id"]), None)
                if waiter is not None:
                    waiter.put(payload)
                continue
            if isinstance(payload, dict):
                self._broadcast(payload)
        self._fail_pending("Codex app-server exited unexpectedly")

    def _request_locked(self, method: str, params: Optional[dict], timeout: float) -> dict:
        if not self._proc_alive():
            with self._lock:
                self._start_locked()
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("Codex app-server is not available")

        request_id = self._next_id
        self._next_id += 1
        waiter: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            with self._write_lock:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise RuntimeError(f"Failed writing request to Codex app-server: {exc}") from exc

        try:
            payload = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Timed out waiting for Codex app-server response: {method}") from exc

        error = payload.get("error") if isinstance(payload, dict) else None
        if error:
            message_text = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"Codex app-server {method} failed: {message_text}")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError(f"Codex app-server {method} returned invalid result payload")
        return result

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        self.ensure_running()
        return self._request_locked(method, params, timeout)

    def register_listener(self) -> "queue.Queue[dict]":
        listener: "queue.Queue[dict]" = queue.Queue()
        with self._lock:
            self._listeners.append(listener)
        return listener

    def unregister_listener(self, listener: "queue.Queue[dict]") -> None:
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def run_turn(
        self,
        *,
        prompt: str,
        model: str,
        effort: str,
        service_tier: str = "",
        timeout: float = 600.0,
        cwd: Optional[str] = None,
    ) -> dict:
        thread_params = {
            "approvalPolicy": "never",
            "cwd": cwd or os.getcwd(),
            "ephemeral": True,
            "model": model,
            "personality": "pragmatic",
            "sandbox": "danger-full-access",
        }
        if service_tier:
            thread_params["serviceTier"] = service_tier
        thread_result = self.request("thread/start", thread_params, timeout=min(timeout, 60.0))
        thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
        if not isinstance(thread, dict):
            raise RuntimeError("Codex app-server thread/start did not return a thread object")
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            raise RuntimeError("Codex app-server thread/start returned an empty thread id")

        listener = self.register_listener()
        start_time = time.time()
        try:
            turn_params = {
                "threadId": thread_id,
                "model": model,
                "effort": effort,
                "input": [{"type": "text", "text": prompt}],
            }
            if service_tier:
                turn_params["serviceTier"] = service_tier
            turn_result = self.request("turn/start", turn_params, timeout=min(timeout, 60.0))
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            if not isinstance(turn, dict):
                raise RuntimeError("Codex app-server turn/start did not return a turn object")
            turn_id = str(turn.get("id") or "").strip()
            if not turn_id:
                raise RuntimeError("Codex app-server turn/start returned an empty turn id")

            assistant_text = ""
            last_usage: dict = {}
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for Codex turn {turn_id} to complete")
                try:
                    notification = listener.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError(f"Timed out waiting for Codex turn {turn_id} notifications") from exc

                method = str(notification.get("method") or "").strip()
                params = notification.get("params") if isinstance(notification, dict) else None
                if not isinstance(params, dict):
                    continue
                if method == "__quaid/process_closed__":
                    raise RuntimeError(str(params.get("message") or "Codex app-server exited"))
                note_thread_id = str(params.get("threadId") or "").strip()
                note_turn_id = str(params.get("turnId") or params.get("turn", {}).get("id") or "").strip()
                if note_thread_id and note_thread_id != thread_id:
                    continue
                if note_turn_id and note_turn_id != turn_id:
                    continue
                if not note_thread_id and not note_turn_id:
                    continue

                if method == "item/completed":
                    text = _extract_agent_text(params.get("item") or {})
                    if text:
                        assistant_text = text
                elif method == "thread/tokenUsage/updated":
                    token_usage = params.get("tokenUsage") or {}
                    if isinstance(token_usage, dict):
                        last_usage = token_usage.get("last") if isinstance(token_usage.get("last"), dict) else {}
                elif method == "turn/completed":
                    completed_turn = params.get("turn") or {}
                    if isinstance(completed_turn, dict) and completed_turn.get("error"):
                        raise RuntimeError(f"Codex turn failed: {completed_turn.get('error')}")
                    return {
                        "text": assistant_text,
                        "duration": time.time() - start_time,
                        "model": model,
                        "usage": last_usage,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "thread_path": str(thread.get("path") or ""),
                    }
        finally:
            self.unregister_listener(listener)


_shared_manager_lock = threading.Lock()
_shared_manager: Optional[_CodexAppServerManager] = None


def get_shared_codex_manager() -> _CodexAppServerManager:
    global _shared_manager
    with _shared_manager_lock:
        if _shared_manager is None:
            _shared_manager = _CodexAppServerManager()
        return _shared_manager


def close_shared_codex_manager() -> None:
    global _shared_manager
    with _shared_manager_lock:
        manager = _shared_manager
        _shared_manager = None
    if manager is not None:
        manager.close()


atexit.register(close_shared_codex_manager)


_BROKER_LOCK = threading.RLock()
_BROKER_CLIENT: Optional["_CodexPlatformBrokerClient"] = None


def _quaid_home_dir() -> Path:
    env = str(os.environ.get("QUAID_HOME", "") or "").strip()
    return Path(env).resolve() if env else (Path.home() / "quaid")


def _broker_run_dir() -> Path:
    d = _quaid_home_dir() / "shared" / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _broker_sock_path() -> Path:
    return _broker_run_dir() / "codex-app-server-broker.sock"


def _broker_pid_path() -> Path:
    return _broker_run_dir() / "codex-app-server-broker.pid"


def _read_broker_pid() -> Optional[int]:
    p = _broker_pid_path()
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        p.unlink(missing_ok=True)
        return None


class _CodexAppServerBroker:
    """Platform-shared broker that owns a single Codex app-server process."""

    def __init__(self):
        self._manager = get_shared_codex_manager()
        self._running = False
        self._server_sock: Optional[socket.socket] = None
        self._lock = threading.RLock()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            buf = ""
            while self._running:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    op = str(msg.get("op") or "").strip()
                    if op == "status":
                        resp = {"ok": True, "pid": os.getpid()}
                    elif op == "run_turn":
                        params = msg.get("params") if isinstance(msg, dict) else None
                        if not isinstance(params, dict):
                            resp = {"ok": False, "error": "Invalid run_turn params"}
                        else:
                            try:
                                result = self._manager.run_turn(
                                    prompt=str(params.get("prompt") or ""),
                                    model=str(params.get("model") or ""),
                                    effort=str(params.get("effort") or ""),
                                    service_tier=str(params.get("service_tier") or ""),
                                    timeout=float(params.get("timeout") or 600.0),
                                    cwd=(str(params.get("cwd")).strip() if params.get("cwd") else None),
                                )
                                resp = {"ok": True, "result": result}
                            except Exception as exc:
                                resp = {"ok": False, "error": str(exc)}
                    else:
                        resp = {"ok": False, "error": f"Unknown op: {op}"}
                    try:
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    except OSError:
                        return

    def run(self) -> None:
        sock_path = _broker_sock_path()
        pid_path = _broker_pid_path()
        sock_path.unlink(missing_ok=True)
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(str(sock_path))
        server_sock.listen(32)
        self._server_sock = server_sock
        self._running = True
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

        def _shutdown(_sig, _frame):
            self._running = False
            try:
                server_sock.close()
            except OSError:
                pass

        import signal
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)

        try:
            while self._running:
                try:
                    conn, _ = server_sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
        finally:
            self._running = False
            pid_path.unlink(missing_ok=True)
            sock_path.unlink(missing_ok=True)
            close_shared_codex_manager()


def _start_broker_process() -> int:
    pid = os.fork()
    if pid == 0:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        os.environ["QUAID_CODEX_BROKER_PROCESS"] = "1"
        try:
            _CodexAppServerBroker().run()
        finally:
            os._exit(0)
    return pid


def ensure_codex_broker_alive() -> int:
    pid = _read_broker_pid()
    if pid is not None:
        return pid
    lock_path = _broker_run_dir() / "codex-app-server-broker-start.lock"
    import fcntl
    fd = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        pid = _read_broker_pid()
        if pid is not None:
            return pid
        child_pid = _start_broker_process()
        sock = _broker_sock_path()
        for _ in range(30):
            if sock.exists():
                return child_pid
            time.sleep(0.1)
        raise RuntimeError("Codex app-server broker did not create socket in time")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()
        lock_path.unlink(missing_ok=True)


class _CodexPlatformBrokerClient:
    """Client for platform-shared Codex app-server broker."""

    def _send(self, msg: dict, timeout: float) -> dict:
        ensure_codex_broker_alive()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(5.0, float(timeout) + 10.0))
            sock.connect(str(_broker_sock_path()))
            sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            buf = ""
            while "\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise RuntimeError("Codex broker connection closed unexpectedly")
                buf += chunk.decode("utf-8", errors="replace")
            line = buf.split("\n")[0].strip()
            if not line:
                raise RuntimeError("Codex broker returned empty response")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise RuntimeError("Codex broker returned invalid response payload")
            if not bool(payload.get("ok")):
                raise RuntimeError(str(payload.get("error") or "Codex broker request failed"))
            return payload.get("result") if isinstance(payload.get("result"), dict) else payload
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def run_turn(
        self,
        *,
        prompt: str,
        model: str,
        effort: str,
        service_tier: str = "",
        timeout: float = 600.0,
        cwd: Optional[str] = None,
    ) -> dict:
        return self._send(
            {
                "op": "run_turn",
                "params": {
                    "prompt": prompt,
                    "model": model,
                    "effort": effort,
                    "service_tier": service_tier,
                    "timeout": timeout,
                    "cwd": cwd or os.getcwd(),
                },
            },
            timeout=timeout,
        )


def get_platform_codex_broker_client() -> _CodexPlatformBrokerClient:
    global _BROKER_CLIENT
    with _BROKER_LOCK:
        if _BROKER_CLIENT is None:
            _BROKER_CLIENT = _CodexPlatformBrokerClient()
        return _BROKER_CLIENT


class CodexLLMProvider(LLMProvider):
    """Routes stateless turns through a shared Codex app-server sidecar."""

    def __init__(
        self,
        *,
        deep_model: str = "gpt-5.4",
        fast_model: str = "gpt-5.4-mini",
        deep_reasoning_effort: str = "high",
        fast_reasoning_effort: str = "none",
        manager: Optional[_CodexAppServerManager] = None,
    ):
        self._deep_model = str(deep_model or "gpt-5.4").strip()
        self._fast_model = str(fast_model or "gpt-5.4-mini").strip()
        self._deep_reasoning_effort = str(deep_reasoning_effort or "high").strip()
        self._fast_reasoning_effort = str(fast_reasoning_effort or "none").strip()
        self._manager = manager

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "fast" and self._fast_model:
            return self._fast_model
        return self._deep_model

    def _resolve_effort(self, model_tier: str) -> str:
        if model_tier == "fast":
            return self._fast_reasoning_effort or "none"
        return self._deep_reasoning_effort or "high"

    def _build_prompt(self, messages: list) -> str:
        sections: List[str] = []
        headings = {
            "system": "System Instructions",
            "user": "User Request",
            "assistant": "Assistant",
        }
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role not in headings:
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text") or "").strip()
                    for part in content
                    if isinstance(part, dict) and str(part.get("text") or "").strip()
                )
            if not isinstance(content, str):
                continue
            content = content.strip()
            if not content:
                continue
            sections.append(f"{headings[role]}:\n{content}")
        prompt = "\n\n".join(sections).strip()
        if not prompt:
            raise ValueError("Cannot make Codex app-server call with empty prompt")
        return prompt

    def llm_call(self, messages, model_tier="deep", max_tokens=4000, timeout=600):
        _ = max_tokens  # turn/start schema does not currently expose an output-token cap.
        prompt = self._build_prompt(messages)
        if self._manager is not None:
            result = self._manager.run_turn(
                prompt=prompt,
                model=self._resolve_model(model_tier),
                effort=self._resolve_effort(model_tier),
                service_tier="fast" if model_tier == "fast" else "",
                timeout=timeout,
                cwd=os.getcwd(),
            )
        else:
            broker = get_platform_codex_broker_client()
            result = broker.run_turn(
                prompt=prompt,
                model=self._resolve_model(model_tier),
                effort=self._resolve_effort(model_tier),
                service_tier="fast" if model_tier == "fast" else "",
                timeout=timeout,
                cwd=os.getcwd(),
            )
        usage = result.get("usage") if isinstance(result, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        return LLMResult(
            text=str(result.get("text") or ""),
            duration=float(result.get("duration") or 0.0),
            input_tokens=int(usage.get("inputTokens", 0) or 0),
            output_tokens=int(usage.get("outputTokens", 0) or 0),
            cache_read_tokens=int(usage.get("cachedInputTokens", 0) or 0),
            model=str(result.get("model") or self._resolve_model(model_tier)),
        )

    def get_profiles(self) -> dict:
        available = True
        try:
            _CodexAppServerManager._resolve_binary()
        except Exception:
            available = False
        return {
            "deep": {"model": self._deep_model, "available": available},
            "fast": {"model": self._fast_model, "available": available},
        }
