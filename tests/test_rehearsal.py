"""Evidence tests, driven through Hermes' real agent loop with scripted tool calls (no model).

T1 on/off · T2 escape across every tool class · T3 fail-closed · T4 apply · T5 two rehearsals at once.
Run with tests/run.sh (it sets HERMES_TEST_HOME, installs the plugin and the test-only plugin).
"""
import json
import os
import shutil
import subprocess
import threading
import time
import unittest

import harness as H
import names
from harness import call


def setUpModule():
    H.require_home()
    H.start_servers()
    assert H.sink_selftest(), "the sink does not log requests: the instrument is broken"


def tearDownModule():
    H.stop_servers()


def _post(path, body="x"):
    return call("terminal", command=f"curl -s -X POST http://127.0.0.1:{H.SINK_PORT}{path} -d '{body}'")


# ------------------------------------------------------------------------------------------- T1
class T1OnOff(unittest.TestCase):
    """BUILD_PROMPT §5 first bullet: on = 0 sink requests and a ledger; off = the sink is hit, no ledger."""

    def test_on_off(self):
        steps = [[call("write_file", path="report.md", content="# Weekly report\nall good\n")],
                 [_post("/t1/webhook", "report ready")],
                 [call("execute_code", code=(
                     "import urllib.request\n"
                     f"urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:{H.SINK_PORT}/t1/code', "
                     "data=b'from code', method='POST')).read()\nprint('posted')\n"))],
                 [call("tool_call", name="testkit_send", arguments={"to": "@sam", "text": "t1 report is ready"})]]
        mark = H.script("t1", steps, "Report written and everyone notified.")
        files = {"notes.txt": "monday\n"}
        ws_on = H.make_ws("t1_on", files)
        ws_off = H.make_ws("t1_off", files)
        before_on = H.tree(ws_on)
        ids_before = H.rehearsal_ids()

        on = H.rehearse(mark, ws_on, "t1_on")
        self.assertEqual(on.returncode, 0, on.stderr[-2000:])
        self.assertEqual(H.sink_requests("/t1"), [], "a request escaped during the rehearsal")
        self.assertEqual(H.sink_with("t1 report is ready"), [], "the outbound plugin tool ran during the rehearsal")
        self.assertEqual(H.tree(ws_on), before_on, "the workspace changed during the rehearsal")
        rows = H.ledger(on.rid)
        classes = sorted(r["class"] for r in rows)
        self.assertIn("file", classes)
        self.assertIn("http", classes)
        self.assertIn("outbound", classes)
        self.assertTrue(any(r["tool"] == "execute_code" for r in rows), "the code's POST was not in the ledger")

        n_ids = len(H.rehearsal_ids())
        off = H.plain_run(mark, ws_off, "t1_off")
        self.assertEqual(off.returncode, 0, off.stderr[-2000:])
        self.assertEqual(len(H.sink_requests("/t1/webhook")), 1, "with the rehearsal off the curl should reach the sink")
        self.assertEqual(len(H.sink_requests("/t1/code")), 1, "with the rehearsal off the code should reach the sink")
        self.assertTrue(os.path.exists(os.path.join(ws_off, "report.md")))
        self.assertEqual(len(H.sink_with("t1 report is ready")), 1, "with the rehearsal off the plugin tool should run")
        self.assertEqual(len(H.rehearsal_ids()), n_ids, "a plain run must not create a rehearsal ledger")
        self.assertEqual(len(ids_before) + 1, n_ids)


# ------------------------------------------------------------------------------------------- T2
class T2Escape(unittest.TestCase):
    """One call per tool class (docs/TOOL_CLASSES.md): nothing outside the clone/overlay changes,
    the sink stays empty, sandbox violations are read from the log and recorded."""

    def test_escape_every_class(self):
        sent = H.path("sentinel")
        with open(os.path.join(sent, "keep.txt"), "w") as f:
            f.write("keep\n")
        child = H.script("t2child", [[call("write_file", path="child.txt", content="child\n")],
                                     [_post("/t2/child", "child")]], "child done")
        steps = [
            [call("read_file", path="README.txt")],                                                # read-only
            [call("search_files", pattern="readme", target="content", path=".")],                   # read-only
            [call("write_file", path="new.txt", content="one\ntwo\n")],                              # file-mutating
            [call("patch", path="README.txt", old_string="readme", new_string="README")],          # file-mutating
            [call("write_file", path=os.path.join(sent, "outside.txt"), content="x\n")],          # file, outside
            [call("terminal", command="echo inside > inside.txt && mv new.txt moved.txt")],         # shell, inside
            [call("terminal", command=f"echo leak > {sent}/shell_leak.txt")],                       # shell, outside write
            [call("terminal", command=f"ln {sent}/keep.txt hardlink.txt")],                         # shell, hard link out
            [call("terminal", command=f"nohup sh -c 'sleep 2; echo late > late.txt' >/dev/null 2>&1 &")],  # background
            [_post("/t2/curl", "hi")],                                                               # outbound via shell
            [call("terminal", command="osascript -e 'display notification \"x\"'")],               # effect binary
            [call("execute_code", code=(f"open('{sent}/code_leak.txt','w').write('x')\n"))],       # shell (code)
            [call("tool_call", name="testkit_send", arguments={"to": "@sam", "text": "t2-send"})],  # outbound (plugin)
            [call("tool_call", name="testkit_mystery", arguments={"note": "x"})],               # unknown effect
            [call("web_extract", urls=[f"http://127.0.0.1:{H.SINK_PORT}/t2/get"])],                 # GET = possible effect
            [call("memory", target="memory", operations=[{"action": "add", "content": "rehearsal fact"}])],  # state
            [call("skill_manage", operations=[{"action": "create", "name": "testkit-skill",
                                               "content": "---\nname: testkit-skill\ndescription: x\n---\nx\n"}])],
            [call("delegate_task", tasks=[{"goal": f"{child} do it", "context": "test"}])],          # subagent
        ]
        mark = H.script("t2", steps, "Everything done.")
        ws = H.make_ws("t2", {"README.txt": "readme\n", "keep/a.txt": "a\n"})
        hermes_state = [os.path.join(H.HOME, d) for d in ("memories", "skills", "cron")]
        before = {"ws": H.tree(ws), "sentinel": H.tree(sent), **{p: H.tree(p) for p in hermes_state}}

        p = H.rehearse(mark, ws, "t2_escape")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        time.sleep(3)  # give a detached child time to try its late write
        after = {"ws": H.tree(ws), "sentinel": H.tree(sent), **{q: H.tree(q) for q in hermes_state}}
        self.assertEqual(after, before, "something outside the clone/overlay changed")
        self.assertEqual(H.sink_requests("/t2"), [], "a network request left during the rehearsal")
        self.assertEqual(H.sink_with("t2-send"), [], "a plugin tool ran during the rehearsal")
        self.assertFalse(os.path.exists(os.path.join(sent, "mystery_ran.txt")))

        rows = H.ledger(p.rid)
        calls = H.lines(os.path.join(H.rdir(p.rid), "calls.jsonl"))
        seen = {c["tool"] for c in calls if c["where"] == "middleware"}
        for tool in ("read_file", "search_files", "write_file", "patch", "terminal", "execute_code", "testkit_send",
                     "testkit_mystery", "web_extract", "memory", "skill_manage", "delegate_task"):
            self.assertIn(tool, seen, f"{tool} never reached the middleware")
        by_cmd = {(r.get("args") or {}).get("command", ""): r for r in rows if r["tool"] == "terminal"}

        def row_for(fragment):
            return next(r for c, r in by_cmd.items() if fragment in c)

        leak = row_for("shell_leak")
        self.assertFalse(leak["previewable"])
        self.assertTrue(leak["preview"]["sandbox"]["blocked_write"], "outside write not read from the sandbox log")
        self.assertEqual(leak["preview"]["sandbox"]["detection"], "unified-log")
        hard = row_for("hardlink")
        self.assertTrue(hard["preview"]["sandbox"]["blocked_write"], "hard link to an outside file not flagged")
        bg = row_for("nohup")
        self.assertFalse(bg["previewable"], "a background process must make the command unpreviewable")
        self.assertIn("starts a background process", bg["preview"]["reasons"])
        osa = row_for("osascript")
        self.assertTrue(osa["preview"]["sandbox"]["blocked_exec"])
        curl = row_for("/t2/curl")
        self.assertEqual(curl["class"], "http")
        self.assertTrue(curl["preview"]["sandbox"]["blocked_network"])
        inside = [r for r in rows if r["class"] == "file" and r["target"]["path"] in ("inside.txt", "moved.txt")]
        self.assertEqual(len(inside), 2, "in-workspace shell changes should be file effects")
        outside = [r for r in rows if r["class"] == "file" and not r["target"]["in_workspace"]]
        self.assertEqual(len(outside), 1)
        self.assertIn("OUTSIDE WORKSPACE", outside[0]["badges"])
        child_rows = [r for r in rows if r["source"].get("task_id") and r["source"]["task_id"] != rows[0]["source"]["task_id"]]
        self.assertTrue(any(r["class"] == "file" and r["target"]["path"] == "child.txt" for r in child_rows),
                        "the subagent's write was not captured")
        self.assertTrue(any(r["class"] == "http" for r in child_rows), "the subagent's POST was not captured")
        for tool in ("testkit_send", "testkit_mystery", "web_extract", "memory", "skill_manage"):
            self.assertTrue(any(r["tool"] == tool and r["class"] in ("outbound", "state", "unknown") for r in rows),
                            f"{tool} not captured")
        # read-after-write inside the rehearsal: the patched README is what later reads see
        with open(os.path.join(H.rdir(p.rid), "work", "README.txt")) as f:
            clone_readme = f.read()
        self.assertEqual(clone_readme, "README\n")
        self.assertFalse(os.path.exists(os.path.join(H.rdir(p.rid), "work", "late.txt")),
                         "a late write from a detached child landed in the clone after the command")


# ------------------------------------------------------------------------------------------- T3
class T3FailClosed(unittest.TestCase):
    """A fault inside the plugin (or in classification, or one that escapes the catch-all) must block
    the call: the real tool does not run."""

    def _run(self, fault):
        mark = H.script(f"t3_{fault}", [[call("write_file", path="leak.txt", content="x\n")],
                                        [_post(f"/t3/{fault}", "x")],
                                        [call("tool_call", name="testkit_send",
                                              arguments={"to": "@a", "text": f"t3-{fault}-send"})]])
        ws = H.make_ws(f"t3_{fault}", {"a.txt": "a\n"})
        before = H.tree(ws)
        p = H.rehearse(mark, ws, f"t3_{fault}", **{names.env("TEST_FAULT"): fault})
        self.assertEqual(H.tree(ws), before)
        self.assertFalse(os.path.exists(os.path.join(ws, "leak.txt")))
        self.assertEqual(H.sink_requests(f"/t3/{fault}"), [], f"fault={fault}: the POST escaped")
        self.assertEqual(H.sink_with(f"t3-{fault}-send"), [], f"fault={fault}: the plugin tool ran")
        rows = H.ledger(p.rid)
        blocked = [r for r in rows if r["class"] == "blocked"]
        self.assertGreaterEqual(len(blocked), 3, f"fault={fault}: expected every call blocked, got {rows}")
        self.assertFalse(os.path.exists(os.path.join(H.rdir(p.rid), "work", "leak.txt")))
        return blocked

    def test_fault_in_middleware(self):
        b = self._run("middleware")
        self.assertTrue(all("injected fault (middleware)" in r["preview"]["error"] for r in b))

    def test_fault_in_classification(self):
        b = self._run("classify")
        self.assertTrue(all("injected fault (classify)" in r["preview"]["error"] for r in b))

    def test_fault_escapes_catch_all_backstop_blocks(self):
        b = self._run("escape")
        self.assertTrue(all("backstop" in r["preview"]["error"] for r in b), b)


# ------------------------------------------------------------------------------------------- T4
def _t4_steps(port):
    return [[call("write_file", path="summary.md", content="# Summary\n3 notes\n")],
            [call("patch", path="notes.txt", old_string="draft", new_string="final")],
            [call("terminal", command="mkdir -p archive && mv old.txt archive/old.txt && rm -f junk.log")],
            [call("read_file", path="summary.md")],
            [call("terminal", command=f"curl -s -X POST http://127.0.0.1:{port}/t4/email -d 'summary: 3 notes'")]]


T4_FILES = {"notes.txt": "draft notes\n", "old.txt": "old\n", "junk.log": "junk\n", "keep.txt": "keep\n"}


class T4Apply(unittest.TestCase):

    def _rehearse(self, name):
        mark = H.script("t4", _t4_steps(H.SINK_PORT), "Summary written, files tidied, email sent.")
        ws = H.make_ws(name, T4_FILES)
        p = H.rehearse(mark, ws, name)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        return ws, p.rid

    def test_deterministic(self):
        ws, rid = self._rehearse("t4_det")
        pristine = H.clone(ws, H.path("ws", "t4_det_pristine"))
        n0 = len(H.sink_requests("/t4/email"))
        a1 = H.apply(rid, ws, "t4_apply1")
        self.assertEqual(a1.returncode, 0, a1.stderr)
        h1 = H.tree(ws)
        self.assertEqual(len(H.sink_requests("/t4/email")), n0 + 1)
        # identical state again: workspace back to pristine, apply state forgotten
        H.move_aside(ws)
        H.clone(pristine, ws)
        H.move_aside(os.path.join(H.rdir(rid), "applied.json"))
        a2 = H.apply(rid, ws, "t4_apply2")
        self.assertEqual(a2.returncode, 0, a2.stderr)
        h2 = H.tree(ws)
        self.assertEqual(h1, h2, "same ledger on identical state gave different trees")
        strip = lambda rc: [(x["id"], x["result"]) for x in rc["results"]]  # noqa: E731
        self.assertEqual(strip(a1.receipt), strip(a2.receipt))
        emails = H.sink_requests("/t4/email")
        self.assertEqual(len(emails), n0 + 2)
        self.assertEqual(emails[-1]["body"], emails[-2]["body"])
        self.assertEqual(H.read(os.path.join(ws, "summary.md")), "# Summary\n3 notes\n")
        self.assertEqual(H.read(os.path.join(ws, "notes.txt")), "final notes\n")
        self.assertTrue(os.path.exists(os.path.join(ws, "archive", "old.txt")))
        self.assertFalse(os.path.exists(os.path.join(ws, "junk.log")))
        # a third apply on the applied state is a no-op and sends nothing again
        a3 = H.apply(rid, ws, "t4_apply3")
        self.assertEqual(a3.receipt["applied"], 0)
        self.assertEqual(H.tree(ws), h2)
        self.assertEqual(len(H.sink_requests("/t4/email")), n0 + 2)

    def test_drift_stops_before_any_change(self):
        ws, rid = self._rehearse("t4_drift")
        with open(os.path.join(ws, "notes.txt"), "w") as f:
            f.write("edited by hand after the rehearsal\n")
        before = H.tree(ws)
        n0 = len(H.sink_requests("/t4/email"))
        a = H.apply(rid, ws, "t4_drift_apply")
        self.assertEqual(a.returncode, 4)
        self.assertEqual(a.receipt["status"], "drift")
        self.assertEqual([d["path"] for d in a.receipt["drift"]], ["notes.txt"])
        self.assertEqual(H.tree(ws), before, "drift must stop the apply before ANY change")
        self.assertEqual(len(H.sink_requests("/t4/email")), n0, "drift must stop before any send")
        text = H.hermes([H.CLI, "apply", rid], cwd=ws, label="t4_drift_text")
        self.assertIn("notes.txt changed since the rehearsal", text.stdout)

    def test_only_except_and_dependents(self):
        ws, rid = self._rehearse("t4_sel")
        rows = H.ledger(rid)
        num = {r["summary"].split(" (")[0]: r["id"] for r in rows}
        summary_id = num["create summary.md"]
        email = next(r for r in rows if r["class"] == "http")
        self.assertIn(summary_id, email["depends_on"], "the email read summary.md: it should depend on it")
        n0 = len(H.sink_requests("/t4/email"))
        a = H.apply(rid, ws, "t4_only", "--only", str(summary_id))
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertTrue(os.path.exists(os.path.join(ws, "summary.md")))
        self.assertEqual(H.read(os.path.join(ws, "notes.txt")), "draft notes\n")
        self.assertTrue(os.path.exists(os.path.join(ws, "old.txt")))
        self.assertEqual(len(H.sink_requests("/t4/email")), n0)
        # the rest, except the delete; the email is ticked while summary is already applied
        delete_id = next(r["id"] for r in rows if r.get("change") == "deleted")
        b = H.apply(rid, ws, "t4_except", "--except", str(delete_id))
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertTrue(os.path.exists(os.path.join(ws, "junk.log")), "--except should have kept junk.log")
        self.assertEqual(H.read(os.path.join(ws, "notes.txt")), "final notes\n")
        self.assertEqual(len(H.sink_requests("/t4/email")), n0 + 1)
        # dependents warning: untick summary.md while the email that read it is ticked
        ws2, rid2 = self._rehearse("t4_dep")
        c = H.apply(rid2, ws2, "t4_dep_dry", "--except", str(summary_id), "--dry-run")
        warned = [w for w in c.receipt["warnings"] if w["id"] == summary_id]
        self.assertTrue(warned, c.receipt)
        self.assertIn(email["id"], warned[0]["dependents"])

    def test_zero_model_calls_instrumented(self):
        ws, rid = self._rehearse("t4_nomodel")
        tag = f"{names.PLUGIN_NAME}-nonet-{rid}"
        prof = os.path.join(H.FIX, "nonet.sb")
        wrap = ["/usr/bin/sandbox-exec", "-D", f"TAG={tag}", "-f", prof]
        trap = f"http://127.0.0.1:{H.TRAP_PORT}"
        proxy = dict(HTTPS_PROXY=trap, HTTP_PROXY=trap, https_proxy=trap, http_proxy=trap,
                     NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        # positive controls: the sandbox logs a blocked internet call; the trap sees a proxied one
        t_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
        ctl = subprocess.Popen(wrap + ["/usr/bin/curl", "-s", "-m", "5", "https://example.com/"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertNotEqual(ctl.wait(), 0, "control: the sandbox let an internet request through")
        ctl_pid = f"({ctl.pid})"  # sandbox-exec execs curl in place, so the denial carries this pid
        trap0 = len(H.lines(H.TRAP_LOG))
        subprocess.run([H.PY, "-c", "import urllib.request,sys\ntry: urllib.request.urlopen('https://openrouter.ai/api/v1/models', timeout=5)\nexcept Exception: pass"],
                       env={**os.environ, **proxy}, capture_output=True)
        self.assertEqual(len(H.lines(H.TRAP_LOG)), trap0 + 1, "control: the proxy trap did not see a proxied call")
        trap1 = len(H.lines(H.TRAP_LOG))
        stub1 = len(H.lines(H.STUB_LOG))
        n0 = len(H.sink_requests("/t4/email"))
        a = H.apply(rid, ws, "t4_nomodel_apply", wrap=wrap, **proxy)
        self.assertEqual(a.returncode, 0, a.stderr[-1500:])
        self.assertEqual(a.receipt["model_calls"], 0)
        self.assertEqual(len(H.lines(H.STUB_LOG)), stub1, "apply called the model endpoint")
        self.assertEqual(len(H.lines(H.TRAP_LOG)), trap1, "apply made a proxied (internet) call")
        self.assertEqual(len(H.sink_requests("/t4/email")), n0 + 1, "the ticked email did not land")
        log = subprocess.run(["/usr/bin/log", "show", "--start", t_start, "--style", "ndjson", "--predicate",
                              f'sender == "Sandbox" AND eventMessage CONTAINS "{tag}"'], capture_output=True, text=True)
        denials = [ln for ln in log.stdout.splitlines() if tag in ln]
        self.assertGreaterEqual(len(denials), 1, "control: the sandbox log shows no denial for the control curl")
        self.assertTrue(any(ctl_pid in ln for ln in denials), "control: no denial logged for the control curl")
        non_control = [ln for ln in denials if ctl_pid not in ln]
        self.assertEqual(non_control, [], f"apply attempted a non-localhost connection: {non_control[:3]}")
        with open(H.path("logs", "t4_nomodel_instrument.json"), "w") as f:
            json.dump({"denials": denials, "trap_before": trap1, "trap_after": len(H.lines(H.TRAP_LOG)),
                       "stub_before": stub1, "stub_after": len(H.lines(H.STUB_LOG)), "receipt": a.receipt}, f, indent=1)


# ------------------------------------------------------------------------------------------- T5
class T5Concurrent(unittest.TestCase):
    """Two rehearsals of the same workspace at the same time do not affect each other or the workspace."""

    def test_two_at_once(self):
        ws = H.make_ws("t5", {"shared.txt": "base\n", "keep.txt": "k\n"})
        before = H.tree(ws)
        marks = {}
        for who in ("A", "B"):
            marks[who] = H.script(f"t5{who}", [
                [call("write_file", path=f"only_{who}.txt", content=f"{who}\n")],
                [call("patch", path="shared.txt", old_string="base", new_string=f"base {who}")],
                [call("terminal", command=f"sleep 1; echo {who} >> shared.txt; cat shared.txt")],
                [_post(f"/t5/{who}", who)]])
        res = {}

        def go(who):
            res[who] = H.rehearse(marks[who], ws, f"t5_{who}")

        th = [threading.Thread(target=go, args=(w,)) for w in ("A", "B")]
        t0 = time.monotonic()
        for t in th:
            t.start()
        for t in th:
            t.join()
        wall = time.monotonic() - t0
        for who in ("A", "B"):
            self.assertEqual(res[who].returncode, 0, res[who].stderr[-1500:])
        self.assertNotEqual(res["A"].rid, res["B"].rid)
        self.assertEqual(H.tree(ws), before, "a rehearsal changed the shared workspace")
        self.assertEqual(H.sink_requests("/t5"), [])
        for who, other in (("A", "B"), ("B", "A")):
            rows = H.ledger(res[who].rid)
            paths = {r["target"]["path"] for r in rows if r["class"] == "file"}
            self.assertIn(f"only_{who}.txt", paths)
            self.assertNotIn(f"only_{other}.txt", paths)
            work = os.path.join(H.rdir(res[who].rid), "work")
            self.assertEqual(H.read(os.path.join(work, "shared.txt")), f"base {who}\n{who}\n")
            self.assertFalse(os.path.exists(os.path.join(work, f"only_{other}.txt")))
            http = [r for r in rows if r["class"] == "http"]
            self.assertEqual([r["target"]["url"] for r in http], [f"http://127.0.0.1:{H.SINK_PORT}/t5/{who}"])
        # Applying both: the second sees the first's change to shared.txt and stops on drift
        a = H.apply(res["A"].rid, ws, "t5_apply_A")
        self.assertEqual(a.returncode, 0, a.stderr)
        b = H.apply(res["B"].rid, ws, "t5_apply_B")
        self.assertEqual(b.returncode, 4)
        self.assertEqual([d["path"] for d in b.receipt["drift"]], ["shared.txt"])
        self.assertFalse(os.path.exists(os.path.join(ws, "only_B.txt")), "drift must stop B before any change")
        self.assertEqual(len(H.sink_requests("/t5/B")), 0)
        with open(H.path("logs", "t5_timing.json"), "w") as f:
            json.dump({"wall_two_parallel_s": round(wall, 2),
                       "each_s": {w: round(res[w].seconds, 2) for w in res}}, f)


# ------------------------------------------------------------------------------------------- session
class TSessionMode(unittest.TestCase):
    """/<slash> on | report | off in a gateway session (resumed by id), then apply from the CLI."""

    def _chat(self, sid, query, label):
        args = ["chat", "-q", query, "--format", "stream-json", "-m", "scripted", "--provider", H.STUB_PROVIDER]
        if sid:
            args[1:1] = ["--resume", sid]
        p = H.hermes(args, cwd=self.ws, label=label)
        results = [json.loads(x) for x in p.stdout.splitlines() if x.startswith("{") and '"result"' in x]
        self.assertTrue(results, p.stdout[-800:] + p.stderr[-800:])
        return results[-1]

    def test_session_on_report_off_apply(self):
        self.ws = H.make_ws("sess", {"a.txt": "a\n"})
        before = H.tree(self.ws)
        mark = H.script("sess", [[call("write_file", path="session.txt", content="from a session\n")],
                                 [_post("/sess/post", "session-post")]], "session task done")
        on = self._chat(None, f"/{names.SLASH_NAME} on {self.ws}", "sess_on")
        self.assertIn("on (r-", on["text"])
        sid = on["session_id"]
        rid = on["text"].split("(", 1)[1].split(")", 1)[0]
        self._chat(sid, f"{mark} do it", "sess_task")
        self.assertEqual(H.tree(self.ws), before)
        self.assertEqual(H.sink_with("session-post"), [])
        rep = self._chat(sid, f"/{names.SLASH_NAME} report", "sess_report")
        self.assertIn("create session.txt", rep["text"])
        off = self._chat(sid, f"/{names.SLASH_NAME} off", "sess_off")
        self.assertIn("off.", off["text"])
        after_task = self._chat(sid, f"{H.script('sess2', [[call('write_file', path='after_off.txt', content='x')]])} go",
                                "sess_after_off")
        self.assertTrue(os.path.exists(os.path.join(self.ws, "after_off.txt")), "after the slash off command tools run normally")
        os.rename(os.path.join(self.ws, "after_off.txt"), H.path("trash", "after_off.txt"))
        a = H.apply(rid, self.ws, "sess_apply")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(H.read(os.path.join(self.ws, "session.txt")), "from a session\n")
        self.assertEqual(len(H.sink_with("session-post")), 1)
        H.hermes(["gateway", "stop"], cwd=self.ws, label="sess_gateway_stop")


if __name__ == "__main__":
    unittest.main()
