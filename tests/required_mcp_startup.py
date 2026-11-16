#!/usr/bin/env python3
"""Actual-binary required-MCP startup regression; synthetic, offline fixtures only.

Runs itself in a fresh user/network namespace containing only loopback.
No preflight or mocked Crustly/provider path is used. A deterministic HTTP
server counts every request, and real stdio subprocesses implement MCP.
The unmodified binary is expected to fail required=true denial assertions.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time


def stdio_server(mode: str, events_path: str) -> int:
    def record(event: str) -> None:
        with open(events_path, "a", encoding="utf-8") as out:
            out.write(json.dumps({"pid": os.getpid(), "event": event}) + "\n")
    record("started")
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        record(str(method))
        if "id" not in message:
            continue
        response = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "initialize" and mode == "init_error":
            response["error"] = {"code": -32000, "message": "fixture initialization denied"}
        elif method == "initialize":
            response["result"] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list" and mode == "discovery_error":
            response["error"] = {"code": -32000, "message": "fixture discovery denied"}
        elif method == "tools/list":
            # Valid empty discovery is intentionally a successful control.
            # This fixture accepts stock clients without initialized so the
            # strict-startup regression is not masked by interoperability.
            response["result"] = {"tools": []}
        else:
            response["error"] = {"code": -32601, "message": "fixture unsupported method"}
        print(json.dumps(response), flush=True)
    record("eof")
    return 0


class FakeModel(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.requests: list[dict] = []
        self.lock = threading.Lock()


class ModelHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def parse_request(self) -> bool:
        accepted = super().parse_request()
        if accepted:
            self.observed_request = {"method": self.command, "path": self.path}
            with self.server.lock:
                self.server.requests.append(self.observed_request)
        return accepted

    def do_GET(self) -> None:
        self.reply({"object": "list", "data": [{"id": "gpt-4o", "object": "model"}]})

    def do_POST(self) -> None:
        # The parser already counted this attempt before body validation.
        request = self.observed_request
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        with self.server.lock:
            request["stream"] = bool(body.get("stream"))
        if body.get("stream"):
            payload = {
                "id": "fixture", "object": "chat.completion.chunk", "created": 0,
                "model": "gpt-4o",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "fixture complete"},
                             "finish_reason": None}],
            }
            finish = {**payload, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            raw = ("data: " + json.dumps(payload) + "\n\n"
                   + "data: " + json.dumps(finish) + "\n\n"
                   + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.reply({
                "id": "fixture", "object": "chat.completion", "created": 0, "model": "gpt-4o",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture complete"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

    def reply(self, value: dict) -> None:
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def enable_isolated_loopback(parent_namespace: str) -> None:
    current = os.readlink("/proc/self/ns/net")
    if not parent_namespace or current == parent_namespace:
        raise RuntimeError("fresh network namespace required")
    if {name for _index, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("fixture namespace must contain loopback only")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("16sh", b"lo", 0)
        flags = struct.unpack("16sh", fcntl.ioctl(sock, 0x8913, request)[:18])[1]
        fcntl.ioctl(sock, 0x8914, struct.pack("16sh", b"lo", flags | 1))
    # Reap orphaned MCP children after the real client exits.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "subreaper setup failed")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def reap_orphans() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
        except ChildProcessError:
            return


def run_case(binary: Path, artifacts: Path, name: str,
             servers: list[tuple[str, bool | None]], denied: bool) -> dict:
    case = artifacts / name
    case.mkdir(mode=0o700)
    fixture_cache = case / "fixture-cache"
    fixture_cache.mkdir(mode=0o700)
    config = case / "config.toml"
    # Prepare only an empty WAL file, not application schema/session state.
    # Native Crustly still opens/migrates the database and executes startup.
    database = sqlite3.connect(case / "fixture.db")
    try:
        database.execute("PRAGMA journal_mode=WAL")
    finally:
        database.close()
    model = FakeModel()
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    model_stopped = False
    events_paths = []
    toml = [
        "[providers.openai]", "enabled = true",
        'base_url = "http://127.0.0.1:' + str(model.server_port) + '/v1"',
        'default_model = "gpt-4o"',
        "[memory]", "enable_codebase_index = false", "project_notes = false", "progress_file = false",
        "[verification]", "enabled = false",
        "[tools]", 'profile = "core"',
    ]
    for index, (mode, required) in enumerate(servers):
        events = case / ("mcp-" + str(index) + ".jsonl")
        events_paths.append(events)
        toml += [
            "[[mcp.servers]]", 'name = "fixture_' + str(index) + '"',
            "command = " + json.dumps(sys.executable),
            "args = " + json.dumps(["-I", "-B", str(Path(__file__).resolve()),
                                    "--stdio-server", mode, "--events", str(events)]),
        ]
        if required is not None:
            toml.append("required = " + str(required).lower())
    config.write_text("\n".join(toml) + "\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "XDG_CONFIG_HOME": str(fixture_cache / "config"),
        "XDG_DATA_HOME": str(fixture_cache / "data"),
        "XDG_CACHE_HOME": str(fixture_cache / "cache"),
        "CRUSTLY_DB_PATH": str(case / "fixture.db"),
        "CRUSTLY_LOG_LEVEL": "error", "NO_COLOR": "1",
        "TOKIO_WORKER_THREADS": "2", "RAYON_NUM_THREADS": "2",
        "OPENAI_API_KEY": "fixture-model-only",
        "OPENAI_BASE_URL": "http://127.0.0.1:" + str(model.server_port) + "/v1",
    }
    # Preserve reserved HOME/CODEX_HOME values; isolate via explicit fixture/cache paths.
    for reserved in ("HOME", "CODEX_HOME"):
        if reserved in os.environ:
            env[reserved] = os.environ[reserved]
    cmd = [str(binary), "--config", str(config), "run",
           "Return the fixture completion.", "--format", "json"]
    process = None
    timed_out = False
    pids: set[int] = set()
    result = {}
    try:
        # Files avoid a descendant-held pipe turning cleanup into an
        # unbounded communicate; timeout remains a separate invalid result.
        with (case / "stdout.txt").open("wb") as out, (case / "stderr.txt").open("wb") as err:
            process = subprocess.Popen(cmd, cwd=case, env=env, stdout=out,
                                       stderr=err, start_new_session=True)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        stdout = (case / "stdout.txt").read_bytes()
        events = []
        for events_path in events_paths:
            if events_path.exists():
                entries = [json.loads(line) for line in events_path.read_text().splitlines()]
                events.append([entry["event"] for entry in entries])
                pids.update(entry["pid"] for entry in entries)
            else:
                events.append([])
        until = time.monotonic() + 2
        while time.monotonic() < until:
            reap_orphans()
            if not any(alive(pid) for pid in pids):
                break
            time.sleep(0.02)
        survivors = [pid for pid in pids if alive(pid)]
        # Freeze the counter after client exit, natural child cleanup and
        # fixture server shutdown, never by killing a client before observation.
        model.shutdown()
        model.server_close()
        thread.join(timeout=3)
        model_stopped = True
        if thread.is_alive():
            raise RuntimeError("fake model server did not stop")
        with model.lock:
            requests = list(model.requests)
        failures = []
        if timed_out:
            failures.append("fixture/client timeout; not a semantic RED")
        if survivors:
            failures.append("MCP child survived client exit")
        if any("initialize" not in entry for entry in events):
            failures.append("MCP initialization fixture was not reached")
        for (mode, _required), entry in zip(servers, events):
            if mode != "init_error" and "tools/list" not in entry:
                failures.append("MCP discovery fixture was not reached")
        if denied:
            if process.returncode == 0:
                failures.append("required MCP failure did not cause nonzero exit")
            if len(requests) != 0:
                failures.append("required MCP failure reached fake model")
        else:
            if process.returncode != 0:
                failures.append("compatibility/control did not exit successfully")
            if len(requests) == 0:
                failures.append("compatibility/control did not reach fake model")
            if b"fixture complete" not in stdout:
                failures.append("fake model completion missing")
        result = {
            "name": name, "expected_denial": denied, "exit": process.returncode,
            "fake_model_requests": len(requests), "requests": requests, "events": events,
            "timed_out": timed_out, "native_child_cleanup": not survivors,
            "fixture_server_stopped": not thread.is_alive(),
            "reserved_env_preserved": all(env.get(key) == os.environ.get(key)
                                          for key in ("HOME", "CODEX_HOME")) ,
            "failures": failures, "passed": not failures,
        }
        return result
    finally:
        # Cleanup is never a preflight or a way to manufacture model count zero.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.wait(timeout=5)
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        reap_orphans()
        if not model_stopped:
            model.shutdown()
            model.server_close()
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError("fake model server did not stop")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--parent-netns")
    parser.add_argument("--stdio-server", choices=["valid", "init_error", "discovery_error"])
    parser.add_argument("--events")
    args = parser.parse_args()
    if args.stdio_server:
        return stdio_server(args.stdio_server, args.events)
    if args.binary is None or args.artifacts is None:
        parser.error("--binary and --artifacts are required")
    os.umask(0o077)
    binary = args.binary.resolve(strict=True)
    artifacts = args.artifacts.resolve()
    if not args.parent_netns:
        unshare = shutil.which("unshare")
        if unshare is None:
            raise RuntimeError("unshare is required for offline fixtures")
        os.execv(unshare, [unshare, "--user", "--map-root-user", "--net", sys.executable,
                          "-I", "-B", str(Path(__file__).resolve()), "--binary", str(binary),
                          "--artifacts", str(artifacts), "--parent-netns",
                          os.readlink("/proc/self/ns/net")])
    enable_isolated_loopback(args.parent_netns)
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
    version = subprocess.check_output([str(binary), "--version"], timeout=5).decode().strip()
    cases = [
        ("required_init_error", [("init_error", True)], True),
        ("required_discovery_error", [("discovery_error", True)], True),
        ("optional_false_init_error", [("init_error", False)], False),
        ("optional_default_discovery_error", [("discovery_error", None)], False),
        ("required_valid_empty_tools", [("valid", True)], False),
        ("required_second_server_error", [("valid", True), ("discovery_error", True)], True),
    ]
    results = []
    for name, servers, denied in cases:
        result = run_case(binary, artifacts, name, servers, denied)
        results.append(result)
        print(json.dumps(result), flush=True)
    report = {
        "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "version": version, "fixture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "network": "fresh user/network namespace; loopback only",
        "results": results,
        "passed": sum(result["passed"] for result in results),
        "failed": sum(not result["passed"] for result in results),
    }
    (artifacts / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
