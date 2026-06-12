"""Minimal exec daemon for the Johnny skills-sandbox container (Johnny-trt.35).

Stdlib-only on purpose: python3 is part of the guaranteed baseline toolset,
so the API server adds zero pip dependencies, zero supply-chain surface, and
stays trivially auditable. It is the ONLY process the container runs.

Endpoints (HTTP, JSON):

    GET  /health            -> {"status": "ok"}
    GET  /bins?names=a,b,c  -> {"bins": {name: bool}, "missing": [...],
                                "all_present": bool}
    POST /exec              -> run one command inside this container
         body:  {"argv": [...]} | {"cmd": "shell string"}   (exactly one)
                optional: "timeout" (seconds), "cwd", "env" ({k: v} overlay)
         reply: {"exit_code", "stdout", "stderr", "truncated",
                 "stdout_truncated", "stderr_truncated", "timed_out",
                 "duration_ms"}

MCP stdio bridge (Johnny-trt.36) — stdio MCP servers run INSIDE this
container (the same security boundary as CLI skills); the worker/api speak
the MCP protocol and pump newline-delimited JSON-RPC over these endpoints:

    POST /mcp/start  {"argv": [...], "env"?: {...}, "cwd"?: "..."}
                     -> {"sid": "..."} (503 at the session cap)
    POST /mcp/send   {"sid": "...", "line": "<one JSON-RPC message>"}
                     -> {"ok": true} | 409 {"exited": true, ...} when dead
    GET  /mcp/recv?sid=...&timeout=20
                     -> long-poll: {"line": "<json-rpc>"} |
                        {"line": null, "exited": bool, "exit_code": int|null,
                         "stderr_tail": "..."}
    POST /mcp/stop   {"sid": "..."} -> {"ok": true, "exit_code": int|null}

Bridge caps (env-tunable): concurrent sessions, per-line byte cap (an
oversized line poisons the session — it is killed, never buffered
unbounded), and an idle reaper that SIGKILLs sessions no endpoint has
touched for SANDBOX_MCP_IDLE_MAX_S (the backstop for a crashed worker;
the worker's own TTL eviction normally stops sessions via /mcp/stop).

Security posture (matches the compose wiring in docker-compose.yml):

* There is deliberately NO auth on this API. It binds 0.0.0.0 but the
  service has no published ports — only containers on the compose network
  (api / worker) can reach it. Do not add a ``ports:`` mapping.
* The process runs as the non-root ``sandbox`` user (Dockerfile USER).
* Per-request caps, all env-tunable (see ``_env_*`` defaults below):
  request body size (-> 413), timeout ceiling (-> 400), per-stream output
  caps (-> ``truncated`` flag, never an error).
* A command still running at its timeout gets SIGKILL delivered to its whole
  process group (``start_new_session=True`` makes pgid == pid), so runaway
  pipelines die with their children.
* Output is accumulated incrementally up to the cap while the rest is
  drained and counted — a runaway ``yes`` cannot balloon daemon memory.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlparse


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


PORT = _env_int("SANDBOX_EXEC_PORT", 8088)
BODY_CAP_BYTES = _env_int("SANDBOX_EXEC_BODY_CAP_BYTES", 256 * 1024)
OUTPUT_CAP_BYTES = _env_int("SANDBOX_EXEC_OUTPUT_CAP_BYTES", 256 * 1024)
TIMEOUT_DEFAULT_S = _env_float("SANDBOX_EXEC_TIMEOUT_DEFAULT_S", 30.0)
TIMEOUT_MAX_S = _env_float("SANDBOX_EXEC_TIMEOUT_MAX_S", 300.0)

# MCP stdio bridge caps (Johnny-trt.36).
MCP_MAX_SESSIONS = _env_int("SANDBOX_MCP_MAX_SESSIONS", 8)
MCP_LINE_CAP_BYTES = _env_int("SANDBOX_MCP_LINE_CAP_BYTES", 1024 * 1024)
MCP_IDLE_MAX_S = _env_float("SANDBOX_MCP_IDLE_MAX_S", 900.0)
MCP_RECV_MAX_WAIT_S = _env_float("SANDBOX_MCP_RECV_MAX_WAIT_S", 30.0)
MCP_STDERR_TAIL_BYTES = 8 * 1024
MCP_QUEUE_MAX_LINES = 256

_READ_CHUNK = 64 * 1024
# After the process group is SIGKILLed every pipe writer is dead and the
# readers see EOF; the join timeout only guards against a grandchild that
# re-setsid'd itself and still holds the pipe open.
_READER_JOIN_S = 5.0


class _BadRequestError(Exception):
    """Client error -> HTTP 400 with the message as ``error``."""


class _StreamReader(threading.Thread):
    """Drain one pipe, keeping at most ``cap`` bytes, counting everything."""

    def __init__(self, stream: BinaryIO, cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self.data = bytearray()
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self._cap - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
        except (OSError, ValueError):
            pass  # pipe torn down by the timeout kill — keep what we have
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    @property
    def truncated(self) -> bool:
        return self.total > len(self.data)

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def _parse_exec_request(payload: dict[str, Any]) -> tuple[list[str], float, str, dict[str, str]]:
    """Validate the /exec body; raise _BadRequestError with a precise message."""
    argv = payload.get("argv")
    cmd = payload.get("cmd")
    if (argv is None) == (cmd is None):
        raise _BadRequestError("provide exactly one of 'argv' (list) or 'cmd' (string)")

    if argv is not None:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise _BadRequestError("'argv' must be a non-empty list of strings")
        final_argv = list(argv)
    else:
        if not isinstance(cmd, str) or not cmd.strip():
            raise _BadRequestError("'cmd' must be a non-empty string")
        final_argv = ["/bin/bash", "-c", cmd]

    timeout_raw = payload.get("timeout", TIMEOUT_DEFAULT_S)
    if not isinstance(timeout_raw, (int, float)) or isinstance(timeout_raw, bool):
        raise _BadRequestError("'timeout' must be a number of seconds")
    timeout = float(timeout_raw)
    if timeout <= 0:
        raise _BadRequestError("'timeout' must be > 0 seconds")
    if timeout > TIMEOUT_MAX_S:
        raise _BadRequestError(
            f"'timeout' {timeout:g}s exceeds the cap of {TIMEOUT_MAX_S:g}s"
        )

    cwd = payload.get("cwd") or os.path.expanduser("~")
    if not isinstance(cwd, str):
        raise _BadRequestError("'cwd' must be a string path")
    if not os.path.isdir(cwd):
        raise _BadRequestError(f"'cwd' does not exist in the sandbox: {cwd}")

    env_overlay = payload.get("env") or {}
    if not isinstance(env_overlay, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_overlay.items()
    ):
        raise _BadRequestError("'env' must be an object of string keys to string values")

    return final_argv, timeout, cwd, env_overlay


def _run_exec(payload: dict[str, Any]) -> dict[str, Any]:
    argv, timeout, cwd, env_overlay = _parse_exec_request(payload)
    env = {**os.environ, **env_overlay}

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        # Mirror shell semantics: a missing binary is a *successful* API call
        # reporting exit 127, so callers handle it like any failed command.
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": f"{argv[0]}: command not found ({exc})",
            "truncated": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
            "duration_ms": 0,
        }
    except PermissionError as exc:
        return {
            "exit_code": 126,
            "stdout": "",
            "stderr": f"{argv[0]}: permission denied ({exc})",
            "truncated": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
            "duration_ms": 0,
        }

    assert proc.stdout is not None and proc.stderr is not None
    out_reader = _StreamReader(proc.stdout, OUTPUT_CAP_BYTES)
    err_reader = _StreamReader(proc.stderr, OUTPUT_CAP_BYTES)
    out_reader.start()
    err_reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()

    out_reader.join(timeout=_READER_JOIN_S)
    err_reader.join(timeout=_READER_JOIN_S)
    duration_ms = int((time.monotonic() - started) * 1000)

    return {
        "exit_code": proc.returncode,
        "stdout": out_reader.text(),
        "stderr": err_reader.text(),
        "truncated": out_reader.truncated or err_reader.truncated,
        "stdout_truncated": out_reader.truncated,
        "stderr_truncated": err_reader.truncated,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def _check_bins(names: list[str]) -> dict[str, Any]:
    bins = {name: shutil.which(name) is not None for name in names}
    missing = [name for name, present in bins.items() if not present]
    return {"bins": bins, "missing": missing, "all_present": not missing}


# --------------------------------------------------------------------------- #
# MCP stdio bridge (Johnny-trt.36)                                            #
# --------------------------------------------------------------------------- #


class _McpLineReader(threading.Thread):
    """Pump one MCP server's stdout into a bounded line queue.

    A line over the byte cap poisons the session (protocol framing is
    newline-delimited JSON-RPC; an unbounded line would balloon memory) —
    the session is marked broken and the process killed. A full queue
    blocks this thread, which fills the pipe buffer, which blocks the
    server: natural backpressure instead of unbounded buffering.
    """

    def __init__(self, session: _McpSession) -> None:
        super().__init__(daemon=True)
        self._session = session

    def run(self) -> None:
        stream = self._session.proc.stdout
        assert stream is not None
        try:
            while True:
                line = stream.readline(MCP_LINE_CAP_BYTES + 1)
                if not line:
                    break
                if len(line) > MCP_LINE_CAP_BYTES:
                    self._session.mark_broken(
                        f"server emitted a line over the {MCP_LINE_CAP_BYTES}-byte cap"
                    )
                    self._session.kill()
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._session.lines.put(text)
        except (OSError, ValueError):
            pass  # pipe torn down by stop/kill — the exit flag tells the story
        finally:
            try:
                stream.close()
            except OSError:
                pass


class _McpStderrReader(threading.Thread):
    """Keep the tail of the server's stderr for the exit/stop diagnostics."""

    def __init__(self, session: _McpSession) -> None:
        super().__init__(daemon=True)
        self._session = session

    def run(self) -> None:
        stream = self._session.proc.stderr
        assert stream is not None
        try:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                self._session.stderr_chunks.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass


class _McpSession:
    """One live stdio MCP server process + its pump threads."""

    def __init__(self, sid: str, argv: list[str], cwd: str, env: dict[str, str]) -> None:
        self.sid = sid
        self.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.lines: queue.Queue[str] = queue.Queue(maxsize=MCP_QUEUE_MAX_LINES)
        # deque of byte chunks; tail-trimmed at read time (cheap, bounded-ish:
        # each chunk is <= _READ_CHUNK and old chunks are dropped on overflow).
        self.stderr_chunks: deque[bytes] = deque(maxlen=64)
        self.last_activity = time.monotonic()
        self.stdin_lock = threading.Lock()
        self.broken_reason: str | None = None
        _McpLineReader(self).start()
        _McpStderrReader(self).start()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def mark_broken(self, reason: str) -> None:
        if self.broken_reason is None:
            self.broken_reason = reason

    @property
    def exited(self) -> bool:
        return self.proc.poll() is not None

    def stderr_tail(self) -> str:
        data = b"".join(self.stderr_chunks)[-MCP_STDERR_TAIL_BYTES:]
        return data.decode("utf-8", errors="replace")

    def send_line(self, line: str) -> None:
        """Write one JSON-RPC message line to the server's stdin (serialized)."""
        stdin = self.proc.stdin
        assert stdin is not None
        with self.stdin_lock:
            stdin.write(line.encode("utf-8") + b"\n")
            stdin.flush()

    def kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self) -> int | None:
        """Graceful stop: close stdin (MCP shutdown), SIGTERM, then SIGKILL."""
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if not self.exited:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.kill()
        try:
            self.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        return self.proc.returncode


_MCP_SESSIONS: dict[str, _McpSession] = {}
_MCP_SESSIONS_LOCK = threading.Lock()


def _mcp_reaper() -> None:
    """Kill sessions nothing has touched for MCP_IDLE_MAX_S (worker-crash backstop)."""
    while True:
        time.sleep(30.0)
        cutoff = time.monotonic() - MCP_IDLE_MAX_S
        with _MCP_SESSIONS_LOCK:
            stale = [s for s in _MCP_SESSIONS.values() if s.last_activity < cutoff]
        for session in stale:
            print(f"[execd] mcp reaper: killing idle session {session.sid}", flush=True)
            session.stop()
            with _MCP_SESSIONS_LOCK:
                _MCP_SESSIONS.pop(session.sid, None)


def _mcp_get(sid: str) -> _McpSession | None:
    with _MCP_SESSIONS_LOCK:
        return _MCP_SESSIONS.get(sid)


def _mcp_start(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    argv = payload.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise _BadRequestError("'argv' must be a non-empty list of strings")
    env_overlay = payload.get("env") or {}
    if not isinstance(env_overlay, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_overlay.items()
    ):
        raise _BadRequestError("'env' must be an object of string keys to string values")
    cwd = payload.get("cwd") or os.path.expanduser("~")
    if not isinstance(cwd, str):
        raise _BadRequestError("'cwd' must be a string path")
    if not os.path.isdir(cwd):
        raise _BadRequestError(f"'cwd' does not exist in the sandbox: {cwd}")

    with _MCP_SESSIONS_LOCK:
        if len(_MCP_SESSIONS) >= MCP_MAX_SESSIONS:
            return 503, {
                "error": (
                    f"mcp session cap reached ({MCP_MAX_SESSIONS}); stop an "
                    "existing session first"
                )
            }
        sid = f"mcp-{secrets.token_hex(8)}"
        try:
            session = _McpSession(sid, list(argv), cwd, {**os.environ, **env_overlay})
        except FileNotFoundError as exc:
            return 400, {"error": f"{argv[0]}: command not found ({exc})"}
        except PermissionError as exc:
            return 400, {"error": f"{argv[0]}: permission denied ({exc})"}
        _MCP_SESSIONS[sid] = session
    print(f"[execd] mcp start: {sid} argv[0]={argv[0]}", flush=True)
    return 200, {"sid": sid}


def _mcp_send(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    sid = payload.get("sid")
    line = payload.get("line")
    if not isinstance(sid, str) or not sid:
        raise _BadRequestError("'sid' must be a non-empty string")
    if not isinstance(line, str) or not line.strip():
        raise _BadRequestError("'line' must be a non-empty string")
    if len(line.encode("utf-8")) > MCP_LINE_CAP_BYTES:
        raise _BadRequestError(f"'line' exceeds the {MCP_LINE_CAP_BYTES}-byte cap")
    if "\n" in line or "\r" in line:
        raise _BadRequestError("'line' must be a single line (no newlines)")
    session = _mcp_get(sid)
    if session is None:
        return 404, {"error": f"unknown mcp session: {sid}"}
    session.touch()
    if session.exited or session.broken_reason:
        return 409, {
            "error": session.broken_reason or "mcp server process has exited",
            "exited": True,
            "exit_code": session.proc.returncode,
            "stderr_tail": session.stderr_tail(),
        }
    try:
        session.send_line(line)
    except (BrokenPipeError, OSError) as exc:
        return 409, {
            "error": f"mcp server stdin closed: {exc}",
            "exited": session.exited,
            "exit_code": session.proc.returncode,
            "stderr_tail": session.stderr_tail(),
        }
    return 200, {"ok": True}


def _mcp_recv(sid: str, timeout_s: float) -> tuple[int, dict[str, Any]]:
    session = _mcp_get(sid)
    if session is None:
        return 404, {"error": f"unknown mcp session: {sid}"}
    session.touch()
    timeout = min(max(timeout_s, 0.0), MCP_RECV_MAX_WAIT_S)
    try:
        line = session.lines.get(timeout=timeout)
        session.touch()
        return 200, {"line": line}
    except queue.Empty:
        return 200, {
            "line": None,
            "exited": session.exited or session.broken_reason is not None,
            "exit_code": session.proc.returncode,
            "stderr_tail": session.stderr_tail() if session.exited else "",
            "error": session.broken_reason or "",
        }


def _mcp_stop(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    sid = payload.get("sid")
    if not isinstance(sid, str) or not sid:
        raise _BadRequestError("'sid' must be a non-empty string")
    with _MCP_SESSIONS_LOCK:
        session = _MCP_SESSIONS.pop(sid, None)
    if session is None:
        return 404, {"error": f"unknown mcp session: {sid}"}
    exit_code = session.stop()
    print(f"[execd] mcp stop: {sid} exit_code={exit_code}", flush=True)
    return 200, {"ok": True, "exit_code": exit_code}


class _Handler(BaseHTTPRequestHandler):
    server_version = "johnny-sandbox-execd/1.0"
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up while we were preparing the reply — routine
            # for the mcp long-poll (a /mcp/recv in flight when the peer
            # closes its transport); nothing to answer, nothing to log loudly.
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path == "/health":
            self._reply(200, {"status": "ok"})
            return
        if url.path == "/bins":
            query = parse_qs(url.query)
            names: list[str] = []
            for raw in query.get("names", []):
                names.extend(n.strip() for n in raw.split(",") if n.strip())
            if not names:
                self._reply(400, {"error": "provide ?names=bin1,bin2,..."})
                return
            self._reply(200, _check_bins(names))
            return
        if url.path == "/mcp/recv":
            query = parse_qs(url.query)
            sid = (query.get("sid") or [""])[0].strip()
            if not sid:
                self._reply(400, {"error": "provide ?sid=..."})
                return
            timeout_raw = (query.get("timeout") or ["20"])[0]
            try:
                timeout_s = float(timeout_raw)
            except ValueError:
                self._reply(400, {"error": "'timeout' must be a number of seconds"})
                return
            status, body = _mcp_recv(sid, timeout_s)
            self._reply(status, body)
            return
        self._reply(404, {"error": f"unknown path: {url.path}"})

    def _read_json_body(self) -> dict[str, Any] | None:
        """Shared body reader for the POST endpoints; replies + returns None on error."""
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            self._reply(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(length_raw)
        except ValueError:
            self._reply(400, {"error": "invalid Content-Length"})
            return None
        if length > BODY_CAP_BYTES:
            self._reply(
                413,
                {
                    "error": (
                        f"request body {length} bytes exceeds the cap of "
                        f"{BODY_CAP_BYTES} bytes"
                    )
                },
            )
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"error": "body must be valid JSON"})
            return None
        if not isinstance(payload, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return None
        return payload

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        handlers = {
            "/exec": lambda payload: (200, _run_exec(payload)),
            "/mcp/start": _mcp_start,
            "/mcp/send": _mcp_send,
            "/mcp/stop": _mcp_stop,
        }
        handler = handlers.get(url.path)
        if handler is None:
            self._reply(404, {"error": f"unknown path: {url.path}"})
            return
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            status, body = handler(payload)
        except _BadRequestError as exc:
            self._reply(400, {"error": str(exc)})
            return
        self._reply(status, body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    server.daemon_threads = True
    threading.Thread(target=_mcp_reaper, daemon=True).start()
    print(
        f"[execd] skills-sandbox exec API on :{PORT} "
        f"(timeout default {TIMEOUT_DEFAULT_S:g}s / max {TIMEOUT_MAX_S:g}s, "
        f"output cap {OUTPUT_CAP_BYTES} B/stream, body cap {BODY_CAP_BYTES} B; "
        f"mcp bridge: {MCP_MAX_SESSIONS} sessions max, idle reap {MCP_IDLE_MAX_S:g}s)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
