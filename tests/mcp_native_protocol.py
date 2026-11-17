#!/usr/bin/env python3
"""Actual-binary MCP lifecycle/schema/discovery regressions; offline fakes only."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

_HELPER_PATH = Path(__file__).with_name("required_mcp_startup.py")
if hashlib.sha256(_HELPER_PATH.read_bytes()).hexdigest() != "b953df0d295b81e34ff957e6ff39fff526f44426fc26544ceeb2501ee4ef655e":
    raise RuntimeError("published startup fixture changed")
_spec = importlib.util.spec_from_file_location("frozen_startup_helpers", _HELPER_PATH)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)

MARKER = "Native MCP schema fixture."
SCHEMA = {"type": "object", "properties": {
    "message": {"type": "string", "enum": ["fixture-value"]}},
    "required": ["message"], "additionalProperties": False}


def stdio_server(mode: str, events: Path) -> int:
    def record(method):
        with events.open("a") as out:
            out.write(json.dumps({"pid": os.getpid(), "method": method}) + "\n")
    record("started")
    initialized = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        record(method)
        if method == "notifications/initialized":
            initialized = True
            continue
        if "id" not in message:
            continue
        response = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "initialize":
            response["result"] = {"protocolVersion": message["params"]["protocolVersion"],
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list" and mode == "strict_initialized" and not initialized:
            response["error"] = {"code": -32002, "message": "fixture initialized required"}
        elif method == "tools/list" and mode == "malformed_discovery":
            response["result"] = {"tools": "fixture-invalid-list"}
        elif method == "tools/list" and mode in ("canonical_schema", "legacy_schema_control"):
            field = "inputSchema" if mode == "canonical_schema" else "input_schema"
            response["result"] = {"tools": [{"name": "probe", "description": MARKER, field: SCHEMA}]}
        elif method == "tools/list":
            response["result"] = {"tools": []}
        else:
            response["error"] = {"code": -32601, "message": "fixture unsupported method"}
        print(json.dumps(response), flush=True)
    return 0


class Model(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.requests = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def parse_request(self):
        accepted = super().parse_request()
        if accepted:
            self.observation = {"method": self.command, "path": self.path}
            with self.server.lock:
                self.server.requests.append(self.observation)
        return accepted

    def reply(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply({"data": [{"id": "gpt-4o", "object": "model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(length))
        with self.server.lock:
            self.observation["body"] = body
        self.reply({"id": "fixture", "object": "chat.completion", "created": 0,
                    "model": "gpt-4o", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "native fixture complete"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def run_case(binary, mode, scratch, artifacts):
    case = scratch / mode
    case.mkdir(mode=0o700)
    db = sqlite3.connect(case / "fixture.db")
    try:
        db.execute("PRAGMA journal_mode=WAL")
    finally:
        db.close()
    server = Model()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    events = case / "mcp-events.jsonl"
    config = case / "config.toml"
    config.write_text("\n".join([
        "[providers.openai]", "enabled = true",
        "base_url = " + json.dumps("http://127.0.0.1:" + str(server.server_port) + "/v1"),
        'default_model = "gpt-4o"', "[memory]", "enable_codebase_index = false",
        "project_notes = false", "progress_file = false", "[verification]", "enabled = false",
        "[tools]", 'profile = "core"', "[[mcp.servers]]", 'name = "compat"',
        "command = " + json.dumps(sys.executable),
        "args = " + json.dumps(["-I", "-B", str(Path(__file__).resolve()),
                                "--stdio-server", mode, "--events", str(events)]),
        "required = true", "",
    ]))
    env = {"PATH": "/usr/bin:/bin", "NO_COLOR": "1",
           "XDG_CONFIG_HOME": str(case / "config"), "XDG_CACHE_HOME": str(case / "cache"),
           "XDG_DATA_HOME": str(case / "data"), "CRUSTLY_DB_PATH": str(case / "fixture.db"),
           "CRUSTLY_LOG_LEVEL": "error", "TOKIO_WORKER_THREADS": "2", "RAYON_NUM_THREADS": "2",
           "OPENAI_API_KEY": "fixture-model-only",
           "OPENAI_BASE_URL": "http://127.0.0.1:" + str(server.server_port) + "/v1"}
    for key in ("HOME", "CODEX_HOME"):
        if key in os.environ:
            env[key] = os.environ[key]
    command = [str(binary), "--config", str(config), "run",
               "Return the fixture completion without calling tools.", "--format", "json"]
    process = None
    pids = set()
    stopped = False
    timed_out = False
    try:
        with (case / "stdout.txt").open("wb") as out, (case / "stderr.txt").open("wb") as err:
            process = subprocess.Popen(command, cwd=case, env=env, stdout=out, stderr=err,
                                       start_new_session=True)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        entries = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
        pids.update(entry["pid"] for entry in entries)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            _helpers.reap_orphans()
            if not any(_helpers.alive(pid) for pid in pids):
                break
            time.sleep(0.02)
        survivors = [pid for pid in pids if _helpers.alive(pid)]
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        stopped = True
        with server.lock:
            requests = list(server.requests)
        methods = [entry["method"] for entry in entries]
        fixture_errors = []
        failures = []
        if timed_out:
            fixture_errors.append("client timeout")
        if survivors or thread.is_alive():
            fixture_errors.append("cleanup incomplete")
        if not all(method in methods for method in ("initialize", "tools/list")):
            fixture_errors.append("native MCP fixture exchange not reached")
        model_bodies = [item["body"] for item in requests if "body" in item]
        parameters = None
        for body in model_bodies:
            for tool in body.get("tools", []):
                function = tool.get("function", {})
                if MARKER in function.get("description", ""):
                    parameters = function.get("parameters")
        if mode == "malformed_discovery":
            if process.returncode == 0:
                failures.append("malformed discovery did not fail required startup")
            if requests:
                failures.append("malformed discovery reached model")
        else:
            if process.returncode != 0:
                failures.append("valid server startup did not succeed")
            if not requests:
                failures.append("valid server did not reach model")
            if b"native fixture complete" not in (case / "stdout.txt").read_bytes():
                failures.append("fake completion missing")
            if mode == "strict_initialized":
                if methods.count("notifications/initialized") != 1:
                    failures.append("initialized notification missing or duplicated")
                elif methods.index("notifications/initialized") > methods.index("tools/list"):
                    failures.append("tools/list preceded initialized")
            if mode in ("canonical_schema", "legacy_schema_control"):
                if parameters is None:
                    fixture_errors.append("schema fixture tool was not advertised")
                elif parameters != SCHEMA:
                    failures.append("tool input schema constraints were not preserved")
        result = {"case": mode, "exit": process.returncode, "fake_model_requests": len(requests),
                  "methods": methods, "advertised_parameters": parameters,
                  "timed_out": timed_out, "native_child_cleanup": not survivors,
                  "server_stopped": not thread.is_alive(),
                  "reserved_env_preserved": all(env.get(k) == os.environ.get(k) for k in ("HOME", "CODEX_HOME")),
                  "fixture_errors": fixture_errors, "contract_failures": failures,
                  "passed": not fixture_errors and not failures}
        target = artifacts / mode
        target.mkdir(mode=0o700)
        for name in ("stdout.txt", "stderr.txt", "mcp-events.jsonl"):
            if (case / name).exists():
                shutil.copyfile(case / name, target / name)
        (target / "model-requests.json").write_text(json.dumps(requests, indent=2))
        (target / "result.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.wait(timeout=5)
        for pid in pids:
            if _helpers.alive(pid):
                os.kill(pid, signal.SIGKILL)
        _helpers.reap_orphans()
        if not stopped:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = ("strict_initialized", "canonical_schema", "malformed_discovery",
             "lenient_empty_control", "legacy_schema_control")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--parent-netns")
    parser.add_argument("--stdio-server", choices=modes)
    parser.add_argument("--events", type=Path)
    args = parser.parse_args()
    if args.stdio_server:
        return stdio_server(args.stdio_server, args.events)
    if not args.binary or not args.artifacts:
        parser.error("--binary and --artifacts are required")
    binary = args.binary.resolve(strict=True)
    artifacts = args.artifacts.resolve()
    if not args.parent_netns:
        os.execv(shutil.which("unshare"), ["unshare", "--user", "--map-root-user", "--net",
                 sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                 "--binary", str(binary), "--artifacts", str(artifacts),
                 "--parent-netns", os.readlink("/proc/self/ns/net")])
    os.umask(0o077)
    _helpers.enable_isolated_loopback(args.parent_netns)
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
    scratch = Path(tempfile.mkdtemp(prefix="mcp-native-protocol-",
                   dir="/dev/shm" if Path("/dev/shm").is_dir() else None))
    try:
        results = []
        for mode in modes:
            result = run_case(binary, mode, scratch, artifacts)
            results.append(result)
            print(json.dumps(result), flush=True)
        report = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                  "fixture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "version": subprocess.check_output([str(binary), "--version"], timeout=5).decode().strip(),
                  "network": "fresh loopback-only network namespace",
                  "storage": "private ephemeral fixture files; no durability claim",
                  "results": results, "passed": sum(row["passed"] for row in results),
                  "failed": sum(not row["passed"] for row in results)}
        (artifacts / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        return 0 if report["failed"] == 0 else 1
    finally:
        shutil.rmtree(scratch)


if __name__ == "__main__":
    raise SystemExit(main())
