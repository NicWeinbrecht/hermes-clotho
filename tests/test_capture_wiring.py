"""Capture details around the edges: refused writes, the home mirror for shell commands, python started from the
terminal, reads that write bookkeeping, late sandbox reports, the provider rule, the report card.

CapRefused      a write Hermes refuses (through a link to an outside file, no read first; a patch with a '..'
                header) leaves a BLOCKED row naming the target and Hermes' message, so the report never says 0
CapHomeMirror   shell commands see HOME as a mirror: `mv x ~/.Trash/` works in the rehearsal and becomes an
                OUTSIDE WORKSPACE row; a command that cannot be previewed puts the mirror back
CapInlinePython python3 -I -c naming a network module is can't-preview, like execute_code
CapBookkeeping  skill_view leaves skills/.usage.json unchanged; kanban_list (it promotes tasks) is held
CapLate         a sandbox report that arrives after its command returned becomes a BLOCKED row at the end
CapProvider     the Claude-subscription provider runs only when its native tools are verified off
CapCard         /clotho report <finished id> answers with the short card
"""
import json
import os
import re
import time
import unittest

import harness as H
import helpers_capture as C
import names
from harness import call

REAL_HOME = os.path.realpath(os.path.expanduser("~"))
CLAUDE = "claude-subscription-directsdk-experimental"


def setUpModule():
    C.start_servers()


def tearDownModule():
    C.stop_servers()


def _top(rows):
    return [(r["id"], r["class"], r["tool"], r["summary"]) for r in rows]


class CapRefused(unittest.TestCase):
    def test_write_through_link_without_read(self):
        out = C.outside("refused_link", {"victim.txt": "victim\n"})
        ws = H.make_ws("cap_refused_link", {"a.txt": "a\n"})
        os.symlink(os.path.join(out, "victim.txt"), os.path.join(ws, "file_link.txt"))
        before = {"ws": H.tree(ws), "out": H.tree(out)}
        mark = H.script("cap_refused_link", [[call("write_file", path="file_link.txt", content="overwritten\n")]])
        p = H.rehearse(mark, ws, "cap_refused_link")
        rows = H.ledger(p.rid)
        C.dump("refused_link", {"rid": p.rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual({"ws": H.tree(ws), "out": H.tree(out)}, before)
        blocked = [r for r in rows if r["class"] == "blocked"]
        self.assertEqual(len(blocked), 1, _top(rows))
        b = blocked[0]
        self.assertEqual(b["tool"], "write_file")
        self.assertEqual(b["target"]["abs_path"], os.path.join(out, "victim.txt"))
        self.assertEqual(b["target"].get("requested_path"), "file_link.txt")
        self.assertIn("OUTSIDE WORKSPACE", b["badges"])
        self.assertIn("VIA SYMLINK", b["badges"])
        self.assertEqual(b["preview"].get("refused_by"), "tool")
        self.assertTrue(b["preview"].get("error"))
        self.assertFalse(b["default_selected"])
        self.assertFalse(b["apply"]["appliable"])

    def test_patch_with_parent_header(self):
        out = C.outside("refused_v4a", {"victim.txt": "victim\n"})
        ws = H.make_ws("cap_refused_v4a", {"a.txt": "alpha\n"})
        patch = (f"*** Begin Patch\n*** Update File: {out}/victim.txt\n@@\n-victim\n+PATCHED\n"
                 "*** Add File: ../escaped.txt\n+escaped\n*** End Patch\n")
        before = {"ws": H.tree(ws), "out": H.tree(out)}
        mark = H.script("cap_refused_v4a", [[call("patch", mode="patch", patch=patch)]])
        p = H.rehearse(mark, ws, "cap_refused_v4a")
        rows = H.ledger(p.rid)
        C.dump("refused_v4a", {"rid": p.rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual({"ws": H.tree(ws), "out": H.tree(out)}, before)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(ws), "escaped.txt")))
        blocked = [r for r in rows if r["class"] == "blocked"]
        self.assertEqual(len(blocked), 1, _top(rows))
        named = [t["requested_path"] for t in blocked[0]["preview"]["targets"]]
        self.assertEqual(named, [f"{out}/victim.txt", "../escaped.txt"])
        self.assertIn("OUTSIDE WORKSPACE", blocked[0]["badges"])
        self.assertTrue(blocked[0]["preview"]["error"])

    def test_overwrite_without_read_inside_the_workspace(self):
        """Seen in testing: Hermes refuses to overwrite doc.md the agent has not read, live and in a
        rehearsal alike, but the rehearsal kept no row for the attempt (the same refusal outside the
        workspace had one)."""
        ws = H.make_ws("cap_refused_inws", {"doc.md": "original\n"})
        before = H.tree(ws)
        mark = H.script("cap_refused_inws", [[call("write_file", path="doc.md", content="rewritten\n")]])
        p = H.rehearse(mark, ws, "cap_refused_inws")
        rows = H.ledger(p.rid)
        C.dump("refused_inws", {"rid": p.rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(H.tree(ws), before)
        work = os.path.join(H.rdir(p.rid), "work")
        self.assertEqual(H.read(os.path.join(work, "doc.md")), "original\n", "Hermes did not refuse (test premise)")
        blocked = [r for r in rows if r["class"] == "blocked"]
        self.assertEqual(len(blocked), 1, _top(rows))
        b = blocked[0]
        self.assertEqual((b["tool"], b["target"]["path"], b["target"]["in_workspace"]), ("write_file", "doc.md", True))
        self.assertNotIn("OUTSIDE WORKSPACE", b["badges"])
        self.assertIn("BLOCKED", b["badges"])
        self.assertEqual(b["preview"].get("refused_by"), "tool")
        self.assertNotIn(work, json.dumps(b), "the clone path leaked into the row")
        self.assertFalse(b["default_selected"])
        self.assertFalse(b["apply"]["appliable"])
        self.assertFalse([r for r in rows if r["class"] == "file"], _top(rows))


class CapFinalText(unittest.TestCase):
    def test_verifier_note_names_the_real_path(self):
        """Seen in testing: Hermes' "File-mutation verifier" note at the end of the agent's text named the
        clone (.../plugin-data/clotho/r-.../work/doc.md) instead of the workspace path."""
        ws = H.make_ws("cap_final_text", {"doc.md": "alpha\nbeta\n"})
        mark = H.script("cap_final_text", [[call("read_file", path="doc.md")],
                                           [call("patch", path="doc.md", old_string="gamma", new_string="delta")]],
                        "I updated doc.md.")
        p = H.rehearse(mark, ws, "cap_final_text")
        text = H.meta(p.rid).get("agent_final_text") or ""
        C.dump("final_text", {"rid": p.rid, "text": text, "stdout": p.stdout[-3000:]})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertIn("File-mutation verifier", text, "Hermes added no verifier note (test premise)")
        data = os.path.join(H.HOME, "plugin-data", names.PLUGIN_NAME)
        for where, s in (("meta", text), ("stdout", p.stdout)):
            self.assertNotIn(data, s, f"{where}: the clone path leaked")
            self.assertNotIn(os.path.join(H.rdir(p.rid), "work"), s, where)
        self.assertIn(os.path.join(ws, "doc.md"), text)


class CapHomeMirror(unittest.TestCase):
    def test_move_to_trash_is_an_outside_effect(self):
        ws = H.make_ws("cap_trash", {"a.txt": "move me\n"})
        name = f"rt-{time.time_ns()}.txt"
        trash = os.path.join(REAL_HOME, ".Trash")
        listing = lambda: sorted(os.listdir(trash)) if os.access(trash, os.R_OK) else None  # noqa: E731
        trash_before, before = listing(), H.tree(ws)
        mark = H.script("cap_trash", [[call("terminal", command=f"mv a.txt ~/.Trash/{name}")],
                                      [call("terminal", command=f"cat ~/.Trash/{name}")]])
        p = H.rehearse(mark, ws, "cap_trash")
        rows = H.ledger(p.rid)
        C.dump("trash", {"rid": p.rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(H.tree(ws), before)
        self.assertEqual(listing(), trash_before, "the real Trash changed")
        files = {(r["target"]["path"], r["change"]): r for r in rows if r["class"] == "file"}
        self.assertIn(("a.txt", "deleted"), files, _top(rows))
        moved = files.get((os.path.join(trash, name), "created"))
        self.assertIsNotNone(moved, _top(rows))
        self.assertFalse(moved["target"]["in_workspace"])
        self.assertIn("OUTSIDE WORKSPACE", moved["badges"])
        self.assertEqual(moved["original"], {"exists": False})
        with open(os.path.join(H.rdir(p.rid), moved["blob"])) as f:
            self.assertEqual(f.read(), "move me\n")
        cmd = next(r for r in rows if r["class"] == "command" and r["args"]["command"].startswith("mv"))
        self.assertIn(moved["id"], cmd["children"])
        sb = cmd["preview"]["sandbox"]
        for k in ("maybe_blocked", "swept", "log_reader", "audit_events"):
            self.assertIn(k, sb)
        self.assertIn("move me", H.read(p.log + ".out.txt") + json.dumps(rows), "the next command did not see the file")

    def test_unpreviewable_command_puts_the_mirror_back(self):
        ws = H.make_ws("cap_trash_back", {"a.txt": "a\n"})
        name = f"rt-back-{time.time_ns()}.txt"
        mark = H.script("cap_trash_back", [
            [call("terminal", command=f"echo x > ~/.Trash/{name} && python3 -I -c 'import socket'")],
            [call("terminal", command=f"ls ~/.Trash/{name} || echo MISSING-{name}")]])
        p = H.rehearse(mark, ws, "cap_trash_back")
        rows = H.ledger(p.rid)
        C.dump("trash_back", {"rid": p.rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        first = rows[0]
        self.assertFalse(first["previewable"], _top(rows))
        self.assertFalse(any(r["class"] == "file" for r in rows), _top(rows))
        self.assertTrue(any(c.get("outside_workspace") for c in first["preview"]["partial_changes_discarded"]),
                        first["preview"])
        self.assertFalse(os.path.exists(os.path.join(H.rdir(p.rid), "home-overlay", "home", ".Trash", name)),
                         "the mirror kept a change of a command that was not previewed")


class CapInlinePython(unittest.TestCase):
    def test_python_c_naming_the_network(self):
        ws = H.make_ws("cap_pyc", {"a.txt": "a\n"})
        mark = H.script("cap_pyc", [
            [call("terminal", command="python3 -I -c 'import urllib.request; print(1)'")],
            [call("terminal", command="python3 -I -c 'print(sum(range(4)))'")]])
        p = H.rehearse(mark, ws, "cap_pyc")
        rows = H.ledger(p.rid)
        C.dump("pyc", {"rid": p.rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(len(rows), 1, _top(rows))
        self.assertIn("urllib", rows[0]["args"]["command"])
        self.assertFalse(rows[0]["previewable"])
        self.assertTrue(any("network" in x for x in rows[0]["preview"]["reasons"]), rows[0]["preview"]["reasons"])


class CapBookkeeping(unittest.TestCase):
    def test_skill_view_leaves_the_home_unchanged(self):
        skill = "clotho-test-skill"  # a skill of the test home's own (a fresh home has none)
        d = os.path.join(H.HOME, "skills", "testing", skill)
        if not os.path.exists(os.path.join(d, "SKILL.md")):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {skill}\ndescription: A skill the tests read.\n---\n\n# Test skill\n\nRead me.\n")
        usage = os.path.join(H.HOME, "skills", ".usage.json")
        read = lambda: H.read(usage) if os.path.exists(usage) else None  # noqa: E731
        ws = H.make_ws("cap_skill", {"a.txt": "a\n"})
        u0 = read()
        mark = H.script("cap_skill", [[call("skill_view", name=skill)]])
        p = H.rehearse(mark, ws, "cap_skill")
        calls = H.lines(os.path.join(H.rdir(p.rid), "calls.jsonl"))
        C.dump("skill", {"rid": p.rid, "skill": skill, "calls": calls})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual([(c["tool"], c["decision"]) for c in calls if c["where"] == "middleware"],
                         [("skill_view", "run")])
        self.assertEqual(read(), u0, "skill_view changed skills/.usage.json during the rehearsal")
        self.assertTrue(any(c.get("where") == "bookkeeping" for c in calls), calls)
        self.assertEqual(H.ledger(p.rid), [])

    def test_kanban_list_is_held(self):
        ws = H.make_ws("cap_kanban", {"a.txt": "a\n"})
        db = [os.path.join(H.HOME, f) for f in ("kanban.db", "kanban.db-wal")]
        stat = lambda: [(os.path.getsize(f), os.stat(f).st_mtime_ns) if os.path.exists(f) else None for f in db]  # noqa
        s0 = stat()
        mark = H.script("cap_kanban", [[call("kanban_list")]])
        p = H.hermes([H.CLI, "run", f"{mark} list", "-m", "scripted", "--provider", H.STUB_PROVIDER, "--no-diffs",
                      "--toolsets", "file,kanban"], cwd=ws, label="cap_kanban")
        rid = re.search(r"(r-\d{8}-\d{6}-[0-9a-f]{4})", p.stderr + p.stdout).group(1)
        rows = H.ledger(rid)
        C.dump("kanban", {"rid": rid, "rows": rows})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual([(r["tool"], r["default_selected"]) for r in rows], [("kanban_list", False)], _top(rows))
        self.assertIn("HELD READ", rows[0]["badges"])
        self.assertEqual(stat(), s0, "the kanban board changed during the rehearsal")


LATE = r'''
import json, os, sys
import helpers_capture as C
C.load_plugin_package()
P = "rt_plugin_under_test"
st, sb, led, nm = (sys.modules[P + "." + m] for m in ("state", "sandbox", "ledger", "names"))
r = st.Rehearsal(st.new_id(), os.environ["CAP_WS"], task="t", mode="run")
r.create()
st.arm_process(r)
tag = f"{nm.PLUGIN_NAME}-{r.id}-1"
res = sb.run(r.work, r.work, "true", r.tmp, tag=tag, orig=r.workspace)
r.cmd_rows[1] = (None, "terminal", "true")
s = sb._streams.get(tag.rsplit("-", 1)[0] + "-")
if s is None:
    print(json.dumps({"skip": "no log stream", "reader": res.get("log_reader")})); sys.exit(0)
with s.cond:
    s.msgs.setdefault(tag, []).append(f"Sandbox: python3.14(4242) deny(1) file-write-create /x/late.txt\n{tag}")
r.closed = True
rows = led.finalize(r.ledger_path, r.reads)
print(json.dumps({"rows": rows}))
'''

PROVIDER = r'''
import json, os, sys
import helpers_capture as C
C.load_plugin_package()
st = sys.modules["rt_plugin_under_test.state"]
cap = sys.modules["rt_plugin_under_test.capture"]
print(json.dumps({"problem": cap.provider_problem(os.environ["CAP_PROVIDER"])}), flush=True)
r = st.Rehearsal(st.new_id(), os.environ["CAP_WS"], task="t", mode="run")
r.create()
st.arm_process(r)
called = []
cap.llm_execution(next_call=lambda req: called.append(1) or {"ok": True}, request={"messages": []},
                  provider=os.environ["CAP_PROVIDER"], base_url="", api_mode="chat_completions")
print(json.dumps({"returned": True, "called": len(called)}), flush=True)
'''

FLAGS = ("command = resolved + ['-p', '--tools', '', '--setting-sources', '', '--strict-mcp-config', "
         "'--max-turns', '1', '--permission-mode', 'dontAsk']\n")


def _claude_home(label, version="0.3.4", flags=FLAGS):
    home = H.path("homes", label)
    d = os.path.join(home, "plugins", CLAUDE)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "plugin.yaml"), "w") as f:
        f.write(f"name: {CLAUDE}\nkind: model-provider\nversion: {version}\n")
    with open(os.path.join(d, "directsdk.py"), "w") as f:
        f.write(flags)
    with open(os.path.join(d, "inert_mcp.py"), "w") as f:
        f.write("DENY = 'Denied: native tools are inert; only Hermes executes tools.'\n")
    return home


class CapLate(unittest.TestCase):
    def test_late_report_becomes_a_blocked_row(self):
        home = H.path("homes", "late")
        os.makedirs(home, exist_ok=True)
        ws = H.make_ws("cap_late", {"a.txt": "a\n"})
        r = C.run_py(LATE, {"HERMES_HOME": home, "CAP_WS": ws, "HERMES_TEST_HOME": H.HOME})
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-1500:]}
        C.dump("late", out)
        if out.get("skip"):
            self.skipTest(out["skip"])
        rows = out.get("rows")
        self.assertIsNotNone(rows, out)
        late = [x for x in rows if x["class"] == "blocked"]
        self.assertEqual(len(late), 1, rows)
        self.assertIn("/x/late.txt", late[0]["summary"])
        self.assertEqual(late[0]["preview"]["violation"]["target"], "/x/late.txt")


class CapProvider(unittest.TestCase):
    def _case(self, label, home, **env):
        ws = H.make_ws(label, {"a.txt": "a\n"})
        r = C.run_py(PROVIDER, {"HERMES_HOME": home, "CAP_WS": ws, "CAP_PROVIDER": CLAUDE,
                                "HERMES_TEST_HOME": H.HOME, **env})
        lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        C.dump(label, {"rc": r.returncode, "out": lines, "err": r.stderr[-1500:]})
        return r, lines

    def test_verified_provider_runs(self):
        r, lines = self._case("cap_prov_ok", _claude_home("prov_ok"))
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(lines[0]["problem"], "")
        self.assertEqual(lines[-1], {"returned": True, "called": 1})

    def test_native_tools_not_verified_off_is_refused(self):
        for label, home, env in (
                ("cap_prov_flags", _claude_home("prov_flags", flags="command = resolved + ['-p']\n"), {}),
                ("cap_prov_version", _claude_home("prov_version", version="0.4.0"), {}),
                ("cap_prov_env", _claude_home("prov_env"), {"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/bin/echo"}),
                ("cap_prov_missing", H.path("homes", "prov_missing"), {})):
            os.makedirs(home, exist_ok=True)
            r, lines = self._case(label, home, **env)
            self.assertEqual(r.returncode, 4, f"{label}: {r.stdout}{r.stderr[-800:]}")
            self.assertTrue(lines and lines[0]["problem"], label)
            self.assertNotIn("returned", r.stdout, f"{label}: the provider was called")
            self.assertIn("refusing to continue", r.stderr, label)


CARD = r'''
import json, os, sys
import helpers_capture as C
C.load_plugin_package()
slash = sys.modules["rt_plugin_under_test.slash"]
report = sys.modules["rt_plugin_under_test.report"]
rid = os.environ["CAP_RID"]
print(json.dumps({"reply": slash.handle(f"report {rid}", session_id="no-such-session"),
                  "card": report.render_card(rid)}))
'''


class CapCard(unittest.TestCase):
    def test_report_of_a_finished_run_is_the_card(self):
        ws = H.make_ws("cap_card", {"a.txt": "a\n"})
        mark = H.script("cap_card", [[call("write_file", path="b.txt", content="b\n")]])
        p = H.rehearse(mark, ws, "cap_card")
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        r = C.run_py(CARD, {"HERMES_HOME": H.HOME, "CAP_RID": p.rid})
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-1500:]}
        C.dump("card", out)
        self.assertIn("reply", out, out)
        self.assertEqual(out["reply"], out["card"])


if __name__ == "__main__":
    unittest.main()
