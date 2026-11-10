"""Helpers for the apply and report unit tests: load the plugin as a package and build synthetic
rehearsals (ledger + meta + blobs) in a temporary data folder, with a fake tool layer.

Nothing here starts Hermes, binds a port or touches the network. The fake tool layer stands in for
Hermes' real tools during apply: it records every call, "sends" by appending to a list, and fails
the way the real tools fail (e.g. curl exit 26 when a --data-binary @file is missing).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "clotho_under_test"


def load_plugin():
    """Import the checkout as a package (the folder name is not importable as is)."""
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, os.path.join(REPO, "__init__.py"),
                                                  submodule_search_locations=[REPO])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


def sub(name):
    load_plugin()
    return importlib.import_module(f"{PKG}.{name}")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fp_bytes(b: bytes, mode="0o644") -> dict:
    return {"exists": True, "kind": "file", "sha256": sha(b), "size": len(b), "mode": mode}


class FakeRun:
    """A synthetic finished rehearsal. Rows are added in order; ids are 1-based."""

    def __init__(self, tmp=None, rid="r-20261110-080000-abcd"):
        self.tmp = tmp or tempfile.mkdtemp(prefix="rt-apply-test-")
        self.data = os.path.join(self.tmp, "data")
        self.ws = os.path.realpath(os.path.join(self.tmp, "ws"))
        os.makedirs(self.ws, exist_ok=True)
        self.rid = rid
        self.dir = os.path.join(self.data, rid)
        os.makedirs(os.path.join(self.dir, "blobs"), exist_ok=True)
        self.rows = []
        self.seq = 0

    # ------------------------------------------------------------ workspace
    def put(self, rel, content: str):
        p = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def live(self, rel):
        p = os.path.join(self.ws, rel)
        return open(p).read() if os.path.exists(p) else None

    # ------------------------------------------------------------ rows
    def _base(self, cls, tool, args, summary, **kw):
        self.seq += 1
        row = {"schema": "clotho.ledger.v1", "rehearsal_id": self.rid, "id": len(self.rows) + 1,
               "class": cls, "tool": tool, "args": args, "source": {"call_seq": self.seq},
               "parent_id": None, "children": [], "summary": summary, "target": None, "change": None,
               "irreversible": False, "previewable": True, "badges": [], "default_selected": True,
               "before": None, "original": None, "original_from": None, "after": None, "blob": None,
               "from_path": None, "vcs_internal": False, "preview": {},
               "apply": {"method": "none", "appliable": True, "reason": None},
               "depends_on": [], "dependents": [], "simulated_result": None, "ts": "2026-11-10T08:00:00+0800"}
        row.update(kw)
        self.rows.append(row)
        return row

    def file(self, rel, content=None, change="created", parent=None, from_path=None, **kw):
        live = os.path.join(self.ws, rel)
        orig_b = None
        if os.path.exists(live):
            orig_b = open(live, "rb").read()
        first = not any(r["class"] == "file" and r["target"]["path"] == rel for r in self.rows)
        original = (fp_bytes(orig_b) if orig_b is not None else {"exists": False}) if first else None
        after, blob = {"exists": False}, None
        if change in ("created", "modified", "renamed"):
            b = (content if content is not None else "").encode()
            after = fp_bytes(b)
            blob = f"blobs/{sha(b)}"
            with open(os.path.join(self.dir, blob), "wb") as f:
                f.write(b)
        method = {"created": "write_blob", "modified": "write_blob", "deleted": "unlink", "renamed": "rename"}[change]
        verb = {"created": "create", "modified": "modify", "deleted": "delete", "renamed": "move"}[change]
        summary = f"{verb} {rel}" if change != "renamed" else f"move {from_path} -> {rel}"
        diff = None
        if content is not None and change in ("created", "modified"):
            diff = f"--- a/{rel}\n+++ b/{rel}\n@@ -0,0 +1,{content.count(chr(10))} @@\n" + "".join(
                "+" + ln + "\n" for ln in content.splitlines())
        orig_from = None
        if change == "renamed":
            src = os.path.join(self.ws, from_path)
            orig_from = fp_bytes(open(src, "rb").read()) if os.path.exists(src) else {"exists": False}
        row = self._base("file", "write_file" if parent is None else "terminal", {"path": rel}, summary,
                         target={"kind": "file", "path": rel, "abs_path": live, "in_workspace": True},
                         change=change, before=original or {"exists": False}, original=original,
                         original_from=orig_from, after=after, blob=blob, from_path=from_path,
                         preview={"diff": diff}, apply={"method": method, "appliable": True, "reason": None},
                         **kw)
        if parent is not None:
            row["parent_id"] = parent["id"]
            parent["children"].append(row["id"])
        return row

    def command(self, command, **kw):
        return self._base("command", "terminal", {"command": command, "workdir": self.ws},
                          f"run `{command}`", target={"kind": "command", "command": command, "cwd": self.ws},
                          apply={"method": "group", "appliable": True,
                                 "reason": "applies the command's file changes listed under it"}, **kw)

    def http(self, command, method, url, body=None, **kw):
        irr = method not in ("GET", "HEAD")
        return self._base("http", "terminal", {"command": command, "workdir": self.ws}, f"{method} {url}",
                          target={"kind": "http", "method": method, "url": url, "headers": [], "body": body},
                          irreversible=irr, badges=(["IRREVERSIBLE"] if irr else []) + ["NETWORK"],
                          preview={"request": {"method": method, "url": url, "headers": [], "body": body}},
                          apply={"method": "command", "appliable": True,
                                 "reason": "re-runs this exact command for real in the workspace"}, **kw)

    def outbound(self, tool, to, text, **kw):
        return self._base("outbound", tool, {"to": to, "message": text}, f"call {tool} -> to={to}",
                          target={"kind": "tool", "name": tool, "recipients": [f"to={to}"]},
                          irreversible=True, badges=["IRREVERSIBLE"],
                          preview={"recipients": [f"to={to}"], "text": text, "args": {"to": to, "message": text}},
                          apply={"method": "tool_call", "appliable": True, "reason": None}, **kw)

    def cant_preview(self, command, why="needs the network", **kw):
        return self._base("command", "terminal", {"command": command, "workdir": self.ws},
                          f"run `{command}` - can't preview: {why}",
                          target={"kind": "command", "command": command, "cwd": self.ws},
                          irreversible=True, previewable=False, default_selected=False,
                          badges=["CAN'T PREVIEW", "IRREVERSIBLE"], preview={"reasons": [why]},
                          apply={"method": "command", "appliable": True,
                                 "reason": "runs for real only when you name it (--only N)"}, **kw)

    def depends(self, row, *on):
        row["depends_on"] = sorted(set(row["depends_on"]) | {r["id"] for r in on})

    def finish(self, **meta):
        for r in self.rows:
            r["dependents"] = sorted(o["id"] for o in self.rows if r["id"] in o["depends_on"])
        with open(os.path.join(self.dir, "ledger.jsonl"), "w") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")
        m = {"schema": "clotho.meta.v1", "id": self.rid, "task": "synthetic task", "workspace": self.ws,
             "status": "finished", "model": "scripted", "model_calls": 3, "tool_calls": len(self.rows),
             "seconds": 4.2, "cost_usd": 0.0}
        m.update(meta)
        with open(os.path.join(self.dir, "meta.json"), "w") as f:
            json.dump(m, f)
        return self.rid


class FakeTools:
    """Stands in for apply._real_tool. ``sent`` lists every call that reached the outside world."""

    def __init__(self, ws):
        self.ws = ws
        self.calls, self.sent = [], []
        self.fail_tool = {}      # tool name -> error text
        self.exit_for = {}       # URL substring -> curl exit code (22 = sent, then an HTTP error)
        self.crash_on = None     # call index (1-based) that raises KeyboardInterrupt before sending

    def __call__(self, name, args, task_id):
        self.calls.append((name, dict(args)))
        if self.crash_on is not None and len(self.calls) == self.crash_on:
            raise KeyboardInterrupt("simulated kill in the middle of apply")
        if name == "terminal":
            cmd = args.get("command", "")
            argv = shlex.split(cmd)
            for i, a in enumerate(argv):
                if a in ("--data-binary", "-d", "--data") and i + 1 < len(argv) and argv[i + 1].startswith("@"):
                    src = os.path.join(args.get("workdir") or self.ws, argv[i + 1][1:])
                    if not os.path.exists(src):
                        return json.dumps({"output": "curl: option --data-binary: error encountered when "
                                                     "reading a file", "exit_code": 26, "error": None})
            m = re.search(r"https?://\S+", cmd)
            url = m.group(0) if m else cmd
            for part, code in self.exit_for.items():
                if part in url:
                    if code == 22:  # the server got it and answered with an error status
                        self.sent.append(("http", url))
                    return json.dumps({"output": f"curl: ({code}) failed", "exit_code": code, "error": None})
            self.sent.append(("http", url))
            return json.dumps({"output": "ok", "exit_code": 0, "error": None})
        if name in self.fail_tool:
            return json.dumps({"error": self.fail_tool[name]})
        self.sent.append((name, args.get("to")))
        return json.dumps({"success": True})


def setup(run: FakeRun, tools: FakeTools = None):
    """Point the loaded plugin at the fake run's data folder and tool layer. Returns (apply, report)."""
    state = sub("state")
    apply = sub("apply")
    report = sub("report")
    state.data_root = lambda: run.data
    apply._real_tool = tools or FakeTools(run.ws)
    return apply, report


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible(s: str) -> str:
    return ANSI_RE.sub("", s)


def width(s: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 0 if unicodedata.combining(ch) else 1
               for ch in visible(s))


def big_run(n=300, tmp=None) -> FakeRun:
    """A synthetic 300-effect rehearsal: a 214-file move, creates with diffs, deletes, web requests,
    messages, a can't-preview command, an unknown tool with a badge the report has never seen, and
    long paths, URLs and bodies that would overflow a narrow terminal."""
    run = FakeRun(tmp)
    for i in range(214):
        run.put(f"photos/IMG_{i:04d}.jpg", f"jpeg {i}\n")
    g = run.command("mkdir -p archive/2026 && mv photos/*.jpg archive/2026/")
    for i in range(214):
        run.file(f"archive/2026/IMG_{i:04d}.jpg", f"jpeg {i}\n", change="renamed", parent=g,
                 from_path=f"photos/IMG_{i:04d}.jpg")
    for i in range(40):
        run.file(f"src/very/deeply/nested/module/folder/component_{i:02d}_with_a_long_name.py",
                 "".join(f"line {j} of a file whose lines are quite long, longer than fifty-eight columns\n"
                         for j in range(8)))
    for i in range(10):
        run.put(f"tmp/cache_{i}.tmp", "x\n")
        run.file(f"tmp/cache_{i}.tmp", change="deleted")
    for i in range(12):
        run.http(f"curl -s -X POST https://hooks.example.invalid/services/T000/B000/XXXXXXXXXXXXXXXX{i} -d hi",
                 "POST", f"https://hooks.example.invalid/services/T000/B000/XXXXXXXXXXXXXXXX{i}",
                 body="a long body line that keeps going well past the fifty-eight column limit of the report " * 2)
    for i in range(10):
        run.outbound("send_message", f"@teammate{i}", "Hi! The nightly job finished. " * 6)
    run.cant_preview("rsync -a ./build/ deploy@203.0.113.7:/srv/www/a/very/long/remote/path/that/overflows/")
    while len(run.rows) < n - 1:
        run.file(f"notes/n{len(run.rows)}.md", "note\n")
    run._base("unknown", "mcp__notes__sync_now", {"x": 1}, "call mcp__notes__sync_now",
              irreversible=True, badges=["IRREVERSIBLE", "UNKNOWN TOOL", "SOME NEW BADGE"], default_selected=False,
              preview={"args": {"x": 1}}, apply={"method": "tool_call", "appliable": True, "reason": None},
              future_field={"ignored": True})
    run.finish()
    return run
