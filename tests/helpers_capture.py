"""Helpers for the capture tests (tests/test_capture_*.py).

* servers that can be started once per test module (stale pid files from an earlier module are moved aside);
* folders outside every workspace, used as sentinels whose hash must not change;
* a second scripted endpoint that also answers Hermes' background memory/skill review;
* a copy-on-write copy of the test home with a changed config (for settings one test needs);
* loading the plugin package in-process without Hermes (for state-level tests).
Nothing here talks to the internet.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time

import harness as H
import names
import snapshot


def start_servers():
    H.require_home()
    for n in ("sink", "stub", "trap"):
        p = H.path("logs", f"{n}.pid")
        if os.path.exists(p):
            H.move_aside(p)
    H._servers.clear()
    H.start_servers()
    assert H.sink_selftest(), "the sink does not log requests: the instrument is broken"


def stop_servers():
    H.stop_servers()
    H._servers.clear()


def outside(name: str, files: dict | None = None) -> str:
    """A folder outside every workspace and every rehearsal folder."""
    d = H.path("outside", name)
    os.makedirs(d, exist_ok=True)
    for rel, content in (files or {}).items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return os.path.realpath(d)


def data_root(home: str | None = None) -> str:
    return os.path.join(home or H.HOME, "plugin-data", names.PLUGIN_NAME)


def top_rows(rows, tool=None):
    return [r for r in rows if not r.get("parent_id") and (tool is None or r["tool"] == tool)]


def middleware_calls(rid, home=None):
    p = os.path.join(data_root(home), rid, "calls.jsonl")
    return [c for c in H.lines(p) if c.get("where") == "middleware"]


# ---------------------------------------------------------------- a home with a changed config
def home_variant(label: str, config_extra: str, provider_port: int | None = None) -> str:
    """APFS clone of the test home with lines appended to its config.yaml. Plugin data starts empty."""
    dst = H.path("homes", label)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src = os.path.realpath(H.HOME)
    snapshot.clonefile(src, dst)
    dst = os.path.realpath(dst)
    # the dependency environment records the home's absolute path: point the copy's records at the copy
    recs = []
    for sub, depth in (("installs", 2), ("source-checks", 1)):
        base = os.path.join(dst, sub)
        for dirpath, _dirs, fnames in os.walk(base):
            if dirpath[len(base):].count(os.sep) >= depth - 1:
                _dirs[:] = []  # do not walk into the environments
            if dirpath[len(base):].count(os.sep) >= depth:
                continue
            recs += [os.path.join(dirpath, f) for f in fnames
                     if f.endswith(".json") and (sub == "source-checks" or f == "facts.json")]
    for rec in recs:
        with open(rec) as f:
            text = f.read()
        if src in text:
            with open(rec + ".tmp", "w") as f:
                f.write(text.replace(src, dst))
            os.replace(rec + ".tmp", rec)
    pd =os.path.join(dst, "plugin-data", names.PLUGIN_NAME)
    if os.path.isdir(pd):
        os.makedirs(H.path("trash"), exist_ok=True)
        os.rename(pd, H.path("trash", f"plugin-data-{label}-{int(time.time() * 1000)}"))
    cfg = os.path.join(dst, "config.yaml")
    text = ""
    if os.path.exists(cfg):
        with open(cfg) as f:
            text = f.read()
    if provider_port:
        text = re.sub(r"(http://127\.0\.0\.1:)\d+(/v1)", lambda m: f"{m.group(1)}{provider_port}{m.group(2)}", text)
    with open(cfg, "w") as f:
        f.write(text.rstrip("\n") + "\n" + config_extra.strip("\n") + "\n")
    return os.path.realpath(dst)


# ---------------------------------------------------------------- a stub that answers the review fork
REVIEW_STUB = r'''
import json, os, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT, SCRIPTS, LOG, PIDF = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
MARK = re.compile(r"\[script:([A-Za-z0-9_\-]+)\]")
REVIEW = "Review the conversation above"
def text_of(m):
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""
def plan(body):
    msgs = body.get("messages") or []
    if not body.get("tools"):
        return None, "Scripted reply.", "notools"
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    if last_user >= 0 and REVIEW in text_of(msgs[last_user]):
        done = any(m.get("role") == "assistant" for m in msgs[last_user:])
        tools = sorted(t.get("function", {}).get("name", "") for t in body.get("tools") or [])
        if done or "memory" not in tools:
            return None, "Nothing to save.", "review-final"
        return [{"name": "memory", "arguments": {"target": "memory", "operations": [
            {"action": "add", "content": "review fork fact"}]}}], None, "review"
    name, start = None, 0
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            mm = MARK.search(text_of(m))
            if mm:
                name, start = mm.group(1), i
    if not name:
        return None, "No script marker found.", "none"
    script = json.load(open(os.path.join(SCRIPTS, name + ".json")))
    k = sum(1 for m in msgs[start:] if m.get("role") == "assistant")
    steps = script.get("steps", [])
    if k < len(steps):
        return steps[k], None, "script"
    return None, script.get("final", "Done."), "final"
class Hd(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send(self, code, data, ctype="application/json"):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "scripted", "object": "model", "context_length": 131072}]})
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        steps, text, kind = plan(body)
        calls = [{"id": f"call_{kind}_{time.time_ns()}_{i}", "type": "function",
                  "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                 for i, c in enumerate(steps or [])]
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "served": [c["function"]["name"] for c in calls]}) + "\n")
        msg = {"role": "assistant", "content": None if calls else text}
        if calls:
            msg["tool_calls"] = calls
        fin = "tool_calls" if calls else "stop"
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        base = {"id": "chatcmpl-x", "created": int(time.time()), "model": body.get("model", "scripted")}
        if not body.get("stream"):
            return self._send(200, dict(base, object="chat.completion", usage=usage,
                                        choices=[{"index": 0, "message": msg, "finish_reason": fin}]))
        delta = {"role": "assistant", "tool_calls": [dict(c, index=i) for i, c in enumerate(calls)]} if calls \
            else {"role": "assistant", "content": text}
        chunks = [dict(base, object="chat.completion.chunk", choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                  dict(base, object="chat.completion.chunk", choices=[{"index": 0, "delta": {}, "finish_reason": fin}], usage=usage)]
        self._send(200, b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n",
                   "text/event-stream")
    def log_message(self, *a):
        pass
open(PIDF, "w").write(str(os.getpid()))
ThreadingHTTPServer(("127.0.0.1", PORT), Hd).serve_forever()
'''


def review_stub_port() -> int:
    return int(os.environ.get(names.env("TEST_REVIEW_STUB_PORT"), str(H.STUB_PORT + 3)))


def start_review_stub():
    """Start the review-aware endpoint (its own port, pid file, request log). Returns (server, log path)."""
    src = H.path("scripts", "review_stub_server.py")
    with open(src, "w") as f:
        f.write(REVIEW_STUB)
    log = H.path("logs", "review_stub.jsonl")
    open(log, "a").close()
    pidf = H.path("logs", "review_stub.pid")
    if os.path.exists(pidf):
        H.move_aside(pidf)
    srv = H.Server("review_stub", [H.PY, "-I", src, str(review_stub_port()), H.path("scripts"), log], review_stub_port())
    srv.start()
    return srv, log


# ---------------------------------------------------------------- the plugin package without Hermes
PKG_DIR_ENV = names.env("TEST_PKG_DIR")  # another copy of the plugin to load (e.g. an older commit, for a before/after)


def load_plugin_package(alias: str = "rt_plugin_under_test"):
    """Import this checkout (or the copy named by PKG_DIR_ENV) as a package, without Hermes on the path."""
    if alias in sys.modules:
        return sys.modules[alias]
    src = os.environ.get(PKG_DIR_ENV) or H.REPO
    spec = importlib.util.spec_from_file_location(alias, os.path.join(src, "__init__.py"),
                                                  submodule_search_locations=[src])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def run_py(code: str, env_extra: dict, timeout=60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run([H.PY, "-B", "-c", code], capture_output=True, text=True, timeout=timeout, env=env,
                          cwd=os.path.join(H.REPO, "tests"))


def dump(label: str, obj) -> str:
    p = H.path("logs", f"cap_{label}.json")
    with open(p, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    return p
