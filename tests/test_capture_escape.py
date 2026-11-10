"""Capture escape tests, driven through Hermes' real agent loop with scripted tool calls (no model).

CapSymlink      in-process write_file / patch through symlinks (parent folder, final file, relative, a link the run
                made itself, a link back into the real workspace): every sentinel hash stays unchanged
CapV4APatch     patch(mode="patch") whose "*** Update/Add/Delete/Move File:" headers name the targets
CapExecuteCode  every execute_code call leaves a ledger row with its code (network, hermes_tools, clean, deletes)
CapBackground   delegate_task(background=true) and Hermes' background memory/skill review fork
"""
import json
import os
import time
import unittest

import harness as H
import helpers_capture as C
from harness import call


def setUpModule():
    C.start_servers()


def tearDownModule():
    C.stop_servers()


def _files(rows):
    return [r for r in rows if r["class"] == "file"]


# ------------------------------------------------------------------------------------------- symlinks
class CapSymlink(unittest.TestCase):
    """Hermes' writer follows symlinks (atomic_write: realpath of a final link; the OS follows linked parents).
    A rehearsal must never let a rehearsed write land outside its clone or outside overlay."""

    def test_symlink_write_through(self):
        out = C.outside("symlink", {"target.txt": "sentinel\n", "deep/keep.txt": "keep\n"})
        ws = H.make_ws("cap_symlink", {"README.txt": "readme\n", "sub/inner_keep.txt": "inner\n"})
        os.symlink(out, os.path.join(ws, "out_link"))                                   # absolute link to a folder
        os.symlink(os.path.join(out, "target.txt"), os.path.join(ws, "file_link.txt"))  # absolute link to a file
        os.symlink(os.path.relpath(out, ws), os.path.join(ws, "rel_link"))              # relative link leaving ws
        os.symlink(os.path.join(ws, "sub"), os.path.join(ws, "self_link"))              # absolute link into ws itself
        steps = [
            [call("write_file", path="out_link/escaped.txt", content="escaped via folder link\n")],
            [call("read_file", path="file_link.txt")],
            [call("write_file", path="file_link.txt", content="overwritten via file link\n")],
            [call("patch", path="out_link/deep/keep.txt", old_string="keep", new_string="PATCHED")],
            [call("write_file", path="rel_link/rel_escaped.txt", content="escaped via relative link\n")],
            [call("terminal", command=f"ln -s {out} made_link")],
            [call("write_file", path="made_link/made.txt", content="escaped via a link made in the run\n")],
            [call("write_file", path="self_link/inner_new.txt", content="lands in the real workspace?\n")],
        ]
        mark = H.script("cap_symlink", steps, "Done.")
        before = {"ws": H.tree(ws), "out": H.tree(out)}
        roots_before = set(os.listdir(C.data_root()))

        p = H.rehearse(mark, ws, "cap_symlink")
        after = {"ws": H.tree(ws), "out": H.tree(out)}
        stray = sorted(set(os.listdir(C.data_root())) - roots_before - {p.rid})
        C.dump("symlink", {"rid": p.rid, "rc": p.returncode, "before": before, "after": after, "stray": stray,
                           "out_listing": sorted(os.listdir(out)), "ws_sub": sorted(os.listdir(os.path.join(ws, "sub")))})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(after["out"], before["out"],
                         f"a rehearsed write landed outside the workspace: {sorted(os.listdir(out))}")
        self.assertEqual(after["ws"], before["ws"],
                         f"a rehearsed write landed in the real workspace: {sorted(os.listdir(os.path.join(ws, 'sub')))}")
        self.assertEqual(stray, [], "a rehearsed write landed in the plugin's data folder outside its rehearsal")

        rows = H.ledger(p.rid)
        files = _files(rows)
        # every write is in the ledger, aimed at the file it would really change
        by_abs = {r["target"]["abs_path"]: r for r in files}
        for name in ("escaped.txt", "target.txt", "deep/keep.txt", "rel_escaped.txt", "made.txt"):
            self.assertIn(os.path.join(out, name), by_abs, f"no effect on {name}: {[r['summary'] for r in rows]}")
            r = by_abs[os.path.join(out, name)]
            self.assertFalse(r["target"]["in_workspace"])
            self.assertIn("OUTSIDE WORKSPACE", r["badges"])
            self.assertIn("VIA SYMLINK", r["badges"])
        inner = by_abs.get(os.path.join(ws, "sub", "inner_new.txt"))
        self.assertIsNotNone(inner, "the write through a link back into the workspace is not in the ledger")
        self.assertTrue(inner["target"]["in_workspace"])
        self.assertEqual(inner["target"]["path"], os.path.join("sub", "inner_new.txt"))
        # the would-be content sits in the rehearsal (overlay / clone), not on disk outside it
        rdir = H.rdir(p.rid)
        with open(os.path.join(rdir, "outside", out.lstrip(os.sep), "target.txt")) as f:
            self.assertEqual(f.read(), "overwritten via file link\n")
        with open(os.path.join(rdir, "outside", out.lstrip(os.sep), "deep", "keep.txt")) as f:
            self.assertEqual(f.read(), "PATCHED\n")
        with open(os.path.join(rdir, "work", "sub", "inner_new.txt")) as f:
            self.assertEqual(f.read(), "lands in the real workspace?\n")
        # the agent was told the path it named, never a clone path
        tr = [r for r in rows if r.get("preview", {}).get("result")]
        self.assertFalse(any(rdir in (r["preview"]["result"] or "") for r in tr))


# ------------------------------------------------------------------------------------------- V4A patches
V4A = """*** Begin Patch
*** Update File: a.txt
@@
-alpha
+ALPHA
*** Add File: added.txt
+brand new
*** Delete File: gone.txt
*** Move File: old.txt -> archive/new.txt
*** Add File: {outside}/v4a_outside.txt
+outside
*** End Patch
"""


class CapV4APatch(unittest.TestCase):
    """patch(mode="patch") takes its targets from the header lines, not from `path`."""

    def _ws(self, name):
        return H.make_ws(name, {"a.txt": "alpha\n", "gone.txt": "bye\n", "old.txt": "old\n", "keep.txt": "keep\n",
                                     "archive/.keep": ""})

    def _check(self, label, with_path):
        out = C.outside(label, {"keep.txt": "keep\n"})
        ws = self._ws(label)
        args = {"mode": "patch", "patch": V4A.format(outside=out)}
        if with_path:
            args["path"] = "a.txt"
        mark = H.script(label, [[call("read_file", path="a.txt")], [call("patch", **args)]], "Patched.")
        before = {"ws": H.tree(ws), "out": H.tree(out)}
        p = H.rehearse(mark, ws, label)
        after = {"ws": H.tree(ws), "out": H.tree(out)}
        rows = H.ledger(p.rid)
        C.dump(label, {"rid": p.rid, "before": before, "after": after, "ws": sorted(os.listdir(ws)),
                       "out": sorted(os.listdir(out)), "rows": [(r["id"], r["class"], r["summary"]) for r in rows]})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(after["ws"], before["ws"], f"the patch changed the real workspace: {sorted(os.listdir(ws))}")
        self.assertEqual(after["out"], before["out"], f"the patch wrote outside the workspace: {sorted(os.listdir(out))}")
        files = {(r["target"]["path"], r["change"]) for r in _files(rows)}
        for want in (("a.txt", "modified"), ("added.txt", "created"), ("gone.txt", "deleted"),
                     ("archive/new.txt", "created"), ("old.txt", "deleted"),
                     (os.path.join(out, "v4a_outside.txt"), "created")):
            self.assertIn(want, files, f"missing effect {want}; ledger: {[r['summary'] for r in rows]}")
        work = os.path.join(H.rdir(p.rid), "work")
        self.assertEqual(H.read(os.path.join(work, "a.txt")), "ALPHA\n")
        self.assertEqual(H.read(os.path.join(work, "added.txt")).rstrip("\n"), "brand new")  # Hermes adds no final newline
        self.assertFalse(os.path.exists(os.path.join(work, "gone.txt")))
        self.assertEqual(H.read(os.path.join(work, "archive", "new.txt")), "old\n")
        a = next(r for r in _files(rows) if r["target"]["path"] == "a.txt")
        self.assertIn("+ALPHA", a["preview"]["diff"])
        return p, rows

    def test_multi_file_patch_with_path(self):
        self._check("cap_v4a_path", True)

    def test_multi_file_patch_without_path(self):
        self._check("cap_v4a_nopath", False)

    def test_multi_file_patch_applies(self):
        p, rows = self._check("cap_v4a_apply", True)
        meta = H.meta(p.rid)
        a = H.apply(p.rid, meta["workspace"], "cap_v4a_apply_do")
        self.assertEqual(a.returncode, 0, a.stdout[-1500:] + a.stderr[-1500:])
        live = meta["workspace"]
        self.assertEqual(H.read(os.path.join(live, "a.txt")), "ALPHA\n")
        self.assertEqual(H.read(os.path.join(live, "added.txt")), H.read(os.path.join(H.rdir(p.rid), "work", "added.txt")))
        self.assertFalse(os.path.exists(os.path.join(live, "gone.txt")))
        self.assertFalse(os.path.exists(os.path.join(live, "old.txt")))
        self.assertEqual(H.read(os.path.join(live, "archive", "new.txt")), "old\n")


# ------------------------------------------------------------------------------------------- execute_code
class CapExecuteCode(unittest.TestCase):
    """An execute_code call never disappears from the ledger, whatever the sandbox log says."""

    def test_every_execute_code_call_has_a_row(self):
        url = f"http://127.0.0.1:{H.SINK_PORT}/cap/code"
        codes = {
            "post_caught": ("import urllib.request\n"
                            "try:\n"
                            f"    urllib.request.urlopen(urllib.request.Request('{url}/caught', data=b'x', method='POST'), timeout=3)\n"
                            "except Exception as e:\n"
                            "    pass\n"
                            "print('ok')\n"),
            "post_raw": ("import urllib.request\n"
                         f"urllib.request.urlopen(urllib.request.Request('{url}/raw', data=b'x', method='POST'), timeout=3)\n"),
            "socket": ("import socket\n"
                       "s = socket.socket()\n"
                       "try:\n"
                       f"    s.connect(('127.0.0.1', {H.SINK_PORT}))\n"
                       "    s.sendall(b'POST /cap/code/socket HTTP/1.0\\r\\nContent-Length: 1\\r\\n\\r\\nx')\n"
                       "except OSError:\n"
                       "    pass\n"),
            "tools": "from hermes_tools import terminal\nterminal('rm -f a.txt b.txt c.txt')\n",
            "clean": "print(sum(range(10)))\n",
            "deletes": "import os\nfor n in ('a.txt', 'b.txt', 'c.txt'):\n    os.remove(n)\nprint('removed')\n",
        }
        steps = [[call("execute_code", code=c)] for c in codes.values()]
        mark = H.script("cap_code", steps, "Done.")
        ws = H.make_ws("cap_code", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
        before = H.tree(ws)
        p = H.rehearse(mark, ws, "cap_code")
        rows = H.ledger(p.rid)
        C.dump("code", {"rid": p.rid, "rows": [(r["id"], r["class"], r["summary"], r.get("badges")) for r in rows]})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(H.tree(ws), before)
        self.assertEqual(H.sink_requests("/cap/code"), [], "a request escaped from execute_code")
        top = C.top_rows(rows, "execute_code")
        self.assertEqual(len(top), len(codes), f"{len(codes)} execute_code calls, {len(top)} top-level rows: "
                                               f"{[r['summary'] for r in rows]}")
        by_code = {}
        for r in top:
            self.assertEqual(r["preview"].get("code"), r["args"]["code"], "the row does not carry the code")
            by_code[r["args"]["code"]] = r
        for k in ("post_caught", "post_raw", "socket", "tools"):
            r = by_code[codes[k]]
            self.assertFalse(r["previewable"], f"{k}: should be CAN'T PREVIEW, got {r['summary']}")
            self.assertIn("CAN'T PREVIEW", r["badges"], k)
            self.assertFalse(r["default_selected"], k)
        clean = by_code[codes["clean"]]
        self.assertTrue(clean["previewable"])
        self.assertIn("45", clean["preview"]["stdout"])
        dele = by_code[codes["deletes"]]
        kids = [x for x in rows if x.get("parent_id") == dele["id"]]
        self.assertEqual(sorted((k["target"]["path"], k["change"]) for k in kids),
                         [("a.txt", "deleted"), ("b.txt", "deleted"), ("c.txt", "deleted")])


class CapCodeReasonsUnit(unittest.TestCase):
    """No Hermes: code that names a network module is can't-preview even when the sandbox log saw nothing
    (the log readback misses some python processes, seen in testing)."""

    def test_reasons_without_any_logged_violation(self):
        C.load_plugin_package()
        import sys
        cap = sys.modules["rt_plugin_under_test.capture"]
        quiet = {"exit_code": 0, "stderr": "", "violations": [], "blocked_network": False}
        self.assertTrue(cap._code_reasons("import urllib.request\nurllib.request.urlopen('http://x')", quiet))
        self.assertTrue(cap._code_reasons("import socket", quiet))
        self.assertTrue(cap._code_reasons("print(1)", {"exit_code": 1, "violations": [],
                                                       "stderr": "PermissionError: [Errno 1] Operation not permitted"}))
        self.assertEqual(cap._code_reasons("print(sum(range(10)))", quiet), [])
        self.assertEqual(cap._code_reasons("open('x.txt','w').write('1')", quiet), [])


# ------------------------------------------------------------------------------------------- background paths
class CapBackground(unittest.TestCase):
    """Regression: background delegation and the background review fork."""

    def test_delegate_task_background_true(self):
        child = H.script("cap_bg_child", [[call("write_file", path="bg_child.txt", content="child\n")],
                                          [call("terminal", command=f"curl -s -X POST http://127.0.0.1:{H.SINK_PORT}"
                                                                    "/cap/bg/child -d child")],
                                          [call("tool_call", name="testkit_send",
                                                arguments={"to": "@bg", "text": "cap-bg-send"})]], "child done")
        mark = H.script("cap_bg", [[call("delegate_task", goal=f"{child} do it", context="test", background=True)]],
                        "Started it in the background.")
        ws = H.make_ws("cap_bg", {"a.txt": "a\n"})
        state_dirs = [os.path.join(H.HOME, d) for d in ("memories", "skills", "cron")]
        before = {"ws": H.tree(ws), **{d: H.tree(d) for d in state_dirs}}
        p = H.rehearse(mark, ws, "cap_bg")
        time.sleep(3)  # a detached child would act after the parent returned
        after = {"ws": H.tree(ws), **{d: H.tree(d) for d in state_dirs}}
        rows = H.ledger(p.rid)
        calls = C.middleware_calls(p.rid)
        C.dump("bg", {"rid": p.rid, "rows": [(r["id"], r["class"], r["summary"], r["source"].get("task_id"))
                                             for r in rows], "calls": calls})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(after, before)
        self.assertEqual(H.sink_requests("/cap/bg"), [], "the background child's POST escaped")
        self.assertEqual(H.sink_with("cap-bg-send"), [], "the background child's plugin send ran")
        self.assertIn("delegate_task", [c["tool"] for c in calls])
        # the child's calls reached the middleware and are in the ledger (captured, or blocked if late)
        for tool in ("write_file", "terminal", "testkit_send"):
            self.assertIn(tool, [c["tool"] for c in calls], f"the child's {tool} never reached the middleware")
        self.assertTrue(any(r["tool"] == "write_file" and (r["class"] == "blocked" or
                                                          r["target"]["path"] == "bg_child.txt") for r in rows))
        self.assertTrue(any(r["class"] in ("http", "blocked") and r["tool"] == "terminal" for r in rows))
        self.assertTrue(any(r["tool"] == "testkit_send" for r in rows))

    def test_background_review_fork(self):
        """With memory.nudge_interval 1, Hermes forks a background review after the turn. The fork's memory
        write must not touch <home>/memories; its call is captured or blocked, never run."""
        srv, log = C.start_review_stub()
        try:
            home = C.home_variant("review_fork", "memory:\n  nudge_interval: 1\n  memory_enabled: true\n",
                                  provider_port=C.review_stub_port())
            mark = H.script("cap_review", [[call("read_file", path="a.txt")]], "Read it.")
            ws = H.make_ws("cap_review", {"a.txt": "remember: the build runs on Fridays\n"})
            mem = os.path.join(home, "memories")
            before = {"ws": H.tree(ws), "mem": H.tree(mem)}
            n0 = len(H.lines(log))
            p = H.rehearse(mark, ws, "cap_review", HERMES_HOME=home)
            time.sleep(3)
            served = H.lines(log)[n0:]
            after = {"ws": H.tree(ws), "mem": H.tree(mem)}
            m = os.path.join(C.data_root(home), p.rid or "none")
            rows = H.lines(os.path.join(m, "ledger.jsonl"))
            calls = H.lines(os.path.join(m, "calls.jsonl"))
            C.dump("review", {"rid": p.rid, "served": served, "calls": calls,
                              "rows": [(r["id"], r["class"], r["tool"], r["summary"]) for r in rows],
                              "before": before, "after": after})
            self.assertEqual(p.returncode, 0, p.stderr[-2000:])
            self.assertEqual(after, before, "the review fork changed memory or the workspace")
            self.assertTrue(any(s["kind"] == "review" for s in served),
                            f"the review fork never asked the model (not exercised): {served}")
            mem_calls = [c for c in calls if c.get("tool") == "memory"]
            self.assertTrue(mem_calls, f"the fork's memory call never reached the plugin: {calls}")
            self.assertTrue(any(r["tool"] == "memory" and r["class"] in ("state", "blocked") for r in rows),
                            f"the fork's memory call is not in the ledger: {[r['summary'] for r in rows]}")
            # D37: Hermes has no switch a plugin can reach to skip the review in one run, so its rows are labelled
            for r in [r for r in rows if r["tool"] == "memory"]:
                self.assertEqual(r["source"].get("origin"), "background_review", r)
                self.assertIn("BACKGROUND REVIEW", r["badges"], r)
                self.assertTrue(r["summary"].startswith("(background review)"), r["summary"])
            task_rows = [r for r in rows if r["tool"] != "memory"]
            self.assertFalse([r for r in task_rows if "BACKGROUND REVIEW" in r["badges"]], task_rows)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
