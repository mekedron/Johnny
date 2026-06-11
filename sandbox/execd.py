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
import shutil
import signal
import subprocess
import threading
import time
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


class _Handler(BaseHTTPRequestHandler):
    server_version = "johnny-sandbox-execd/1.0"
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        self._reply(404, {"error": f"unknown path: {url.path}"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path != "/exec":
            self._reply(404, {"error": f"unknown path: {url.path}"})
            return

        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            self._reply(411, {"error": "Content-Length required"})
            return
        try:
            length = int(length_raw)
        except ValueError:
            self._reply(400, {"error": "invalid Content-Length"})
            return
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
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise _BadRequestError("body must be a JSON object")
            result = _run_exec(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"error": "body must be valid JSON"})
            return
        except _BadRequestError as exc:
            self._reply(400, {"error": str(exc)})
            return
        self._reply(200, result)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    server.daemon_threads = True
    print(
        f"[execd] skills-sandbox exec API on :{PORT} "
        f"(timeout default {TIMEOUT_DEFAULT_S:g}s / max {TIMEOUT_MAX_S:g}s, "
        f"output cap {OUTPUT_CAP_BYTES} B/stream, body cap {BODY_CAP_BYTES} B)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
