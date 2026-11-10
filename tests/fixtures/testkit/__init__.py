"""Test-only plugin: two tools with real effects, so the suite can prove they are captured, not run.

testkit_send    POSTs its text to the local HTTP sink (stands in for email / chat / webhooks).
testkit_mystery writes a marker file into the sentinel folder (stands in for any tool of unknown effect).
Both read their targets from environment variables set by the tests; they never reach the internet.
"""
import json
import os
import urllib.request

SEND = {"name": "testkit_send", "description": "Send a message to a recipient (test sink).",
        "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                       "required": ["to", "text"]}}
MYSTERY = {"name": "testkit_mystery", "description": "Do something (test tool of unknown effect).",
           "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}}


def _send(args, **kw):
    port = int(os.environ.get("TESTKIT_SINK_PORT", "0"))
    if not port:
        return json.dumps({"error": "TESTKIT_SINK_PORT not set"})
    data = json.dumps({"to": args.get("to"), "text": args.get("text")}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/testkit/send", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.dumps({"sent": True, "status": resp.status})


def _mystery(args, **kw):
    d = os.environ.get("TESTKIT_SENTINEL_DIR")
    if not d:
        return json.dumps({"error": "TESTKIT_SENTINEL_DIR not set"})
    with open(os.path.join(d, "mystery_ran.txt"), "a") as f:
        f.write(str(args.get("note")) + "\n")
    return json.dumps({"done": True})


def register(ctx):
    ctx.register_tool(name="testkit_send", toolset="testkit", schema=SEND, handler=_send)
    ctx.register_tool(name="testkit_mystery", toolset="testkit", schema=MYSTERY, handler=_mystery)
