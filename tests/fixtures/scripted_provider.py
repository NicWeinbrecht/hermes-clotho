"""A scripted OpenAI-compatible endpoint for tests: no model, no inference.

It replays a fixed list of tool calls so the plugin can be driven through Hermes' real agent
loop and tool registry without spending anything. Usage:

    python scripted_provider.py <port> <scripts_dir> <request_log.jsonl> <pid_file>

The first user message names the script with a marker such as ``[script:t1_onoff]``;
``<scripts_dir>/t1_onoff.json`` holds ``{"steps": [[{"name": ..., "arguments": {...}}, ...], ...]}``.
Step k is served when the request already holds k assistant messages after the user message
that carries the marker, so the endpoint is
stateless and safe for several runs at once. After the last step it answers with plain text.
Requests without tools (titles, summaries) get a short plain-text answer.
"""
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, SCRIPTS, LOG, PIDF = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
MARK = re.compile(r"\[script:([A-Za-z0-9_\-]+)\]")


def _text_of(msg):
    c = msg.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _last_tool_results(body):
    """The tool results the model is answering in this request (after its last turn), cut to 4000 chars."""
    out = []
    for m in reversed(body.get("messages") or []):
        if m.get("role") != "tool":
            break
        out.append(_text_of(m)[:4000])
    return list(reversed(out)) or None


def _plan(body):
    msgs = body.get("messages") or []
    if not body.get("tools"):
        return None, "Scripted reply."
    name, start = None, 0
    for i, m in enumerate(msgs):  # the LAST user message with a marker starts the script (resumed sessions)
        if m.get("role") == "user":
            mm = MARK.search(_text_of(m))
            if mm:
                name, start = mm.group(1), i
    if not name:
        return None, "No script marker found."
    with open(os.path.join(SCRIPTS, name + ".json")) as f:
        script = json.load(f)
    k = sum(1 for m in msgs[start:] if m.get("role") == "assistant")
    steps = script.get("steps", [])
    if k < len(steps):
        calls = []
        for i, c in enumerate(steps[k]):
            calls.append({"id": f"call_{name}_{k}_{i}", "type": "function",
                          "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}})
        return calls, None
    return None, script.get("final", "Done (scripted).")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._log({"method": "GET", "path": self.path})
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "scripted", "object": "model",
                                                                 "context_length": 131072}]})
        return self._send(404, {"error": "not found"})

    def _log(self, rec):
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.time(), **rec}) + "\n")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        calls, text = _plan(body)
        self._log({"method": "POST", "path": self.path, "stream": bool(body.get("stream")),
                   "has_tools": bool(body.get("tools")), "n_messages": len(body.get("messages") or []),
                   "served": [c["function"]["name"] for c in calls] if calls else None,
                   "tools": sorted(t.get("function", {}).get("name", "") for t in body.get("tools") or [])
                   if len(body.get("messages") or []) <= 2 else None,
                   "tool_results": _last_tool_results(body)})
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        msg = {"role": "assistant", "content": text if not calls else None}
        if calls:
            msg["tool_calls"] = calls
        fin = "tool_calls" if calls else "stop"
        if not body.get("stream"):
            return self._send(200, {"id": "chatcmpl-scripted", "object": "chat.completion", "created": int(time.time()),
                                    "model": body.get("model", "scripted"),
                                    "choices": [{"index": 0, "message": msg, "finish_reason": fin}], "usage": usage})
        chunks = []
        base = {"id": "chatcmpl-scripted", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": body.get("model", "scripted")}
        if calls:
            delta = {"role": "assistant", "tool_calls": [dict(c, index=i) for i, c in enumerate(calls)]}
        else:
            delta = {"role": "assistant", "content": text}
        chunks.append(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]))
        chunks.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": fin}], usage=usage))
        data = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
        self._send(200, data, "text/event-stream")

    def log_message(self, *a):
        pass


open(PIDF, "w").write(str(os.getpid()))
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
