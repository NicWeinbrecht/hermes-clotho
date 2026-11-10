"""Several rehearsals at once in one HERMES_HOME, and the refusal to arm under an ACP provider.

CapIds          3 processes claim a rehearsal folder at the same instant with the SAME proposed id: each ends
                up with its own folder and id; meta.json carries the fork_id / timeline labels it was given
CapConcurrent   3 `hermes clotho run` processes at once, one workspace, one home, forced id collision:
                3 ids, each ledger and clone holds only its own effects, workspace and sink untouched
CapAcp          an ACP provider is refused: by name before the model starts, and at the first model call
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest

import harness as H
import helpers_capture as C
import names
from harness import call

RID_RE = re.compile(r"^" + re.escape(names.RID_PREFIX) + r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
FORCED = names.RID_PREFIX + "20261110-000000-abcd"


def setUpModule():
    C.start_servers()


def tearDownModule():
    C.stop_servers()


CREATE = r'''
import json, os, sys, time
import helpers_capture as C
C.load_plugin_package()
st = sys.modules["rt_plugin_under_test.state"]
go = os.environ["CAP_GO"]
while not os.path.exists(go):
    time.sleep(0.001)
try:
    r = st.Rehearsal(os.environ["CAP_RID"], os.environ["CAP_WS"], task="t", mode="run")
    r.create()
    print(json.dumps({"ok": True, "id": r.id, "dir": r.dir, "meta": json.load(open(r.meta_path))}))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
'''


class CapIds(unittest.TestCase):
    def test_three_processes_same_proposed_id(self):
        home = H.path("homes", "ids")
        os.makedirs(home, exist_ok=True)
        ws = H.make_ws("cap_ids", {"a.txt": "a\n"})
        go = H.path("scripts", f"go_{time.time_ns()}")
        procs = []
        for i in range(3):
            env = dict(os.environ, HERMES_HOME=home, CAP_WS=ws, CAP_GO=go, HERMES_TEST_HOME=H.HOME, CAP_RID=FORCED,
                       **{names.env("FORK_ID"): "fork-1",
                          names.env("TIMELINE"): f"timeline-{i}"})
            procs.append(subprocess.Popen([H.PY, "-B", "-c", CREATE], env=env, cwd=os.path.join(H.REPO, "tests"),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        time.sleep(1.0)  # all three are waiting on the go file
        open(go, "w").close()
        outs = []
        for p in procs:
            o, e = p.communicate(timeout=60)
            outs.append(json.loads(o.strip().splitlines()[-1]) if o.strip() else {"ok": False, "error": e[-800:]})
        C.dump("ids", outs)
        self.assertTrue(all(o["ok"] for o in outs), outs)
        ids = [o["id"] for o in outs]
        self.assertEqual(len(set(ids)), 3, ids)
        self.assertEqual(ids.count(FORCED), 1, ids)
        for o in outs:
            self.assertRegex(o["id"], RID_RE)
            self.assertEqual(o["meta"]["id"], o["id"])
            self.assertEqual(o["meta"]["fork_id"], "fork-1")
            self.assertTrue(os.path.isdir(os.path.join(o["dir"], "work")))
        self.assertEqual(sorted(o["meta"]["timeline"] for o in outs), ["timeline-0", "timeline-1", "timeline-2"])
        self.assertEqual(sorted(os.listdir(C.data_root(home))), sorted(ids))


class CapConcurrent(unittest.TestCase):
    def test_three_rehearsals_at_once(self):
        ws = H.make_ws("cap_conc", {"shared.txt": "original\n", "keep.txt": "keep\n"})
        before = H.tree(ws)
        marks = {}
        for i in range(3):
            marks[i] = H.script(f"cap_conc_{i}", [
                [call("read_file", path="shared.txt")],
                [call("write_file", path="shared.txt", content=f"edit-{i}\n")],
                [call("write_file", path=f"own_{i}.txt", content=f"own {i}\n")],
                [call("terminal", command=f"curl -s -X POST http://127.0.0.1:{H.SINK_PORT}/cap/conc/{i} -d x{i}")],
                [call("terminal", command="cat shared.txt")],
            ], f"timeline {i} done")
        results = {}

        def go(i):
            results[i] = H.rehearse(marks[i], ws, f"cap_conc_{i}",
                                       **{names.env("TEST_RID"): FORCED,
                                          names.env("FORK_ID"): "fork-conc", names.env("TIMELINE"): f"t{i}"})
        t0 = time.monotonic()
        threads = [threading.Thread(target=go, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        wall = time.monotonic() - t0
        info = {i: {"rc": p.returncode, "rid": p.rid, "secs": round(p.seconds, 2), "err": p.stderr[-600:]}
                for i, p in results.items()}
        C.dump("concurrent", {"wall": round(wall, 2), "runs": info})
        for i, p in results.items():
            self.assertEqual(p.returncode, 0, f"run {i}: {p.stderr[-1500:]}")
        rids = [results[i].rid for i in range(3)]
        self.assertEqual(len(set(rids)), 3, rids)
        self.assertEqual(H.tree(ws), before, "the workspace changed")
        self.assertEqual(H.sink_requests("/cap/conc"), [], "a request escaped")
        for i in range(3):
            rid = results[i].rid
            m = H.meta(rid)
            self.assertEqual((m["fork_id"], m["timeline"]), ("fork-conc", f"t{i}"))
            rows = H.ledger(rid)
            paths = sorted(r["target"]["path"] for r in rows if r["class"] == "file")
            self.assertEqual(paths, sorted(["shared.txt", f"own_{i}.txt"]), f"run {i} ledger: {paths}")
            urls = [r["target"]["url"] for r in rows if r["class"] == "http"]
            self.assertEqual(urls, [f"http://127.0.0.1:{H.SINK_PORT}/cap/conc/{i}"])
            work = os.path.join(H.rdir(rid), "work")
            self.assertEqual(H.read(os.path.join(work, "shared.txt")), f"edit-{i}\n")
            self.assertEqual(sorted(f for f in os.listdir(work) if f.startswith("own_")), [f"own_{i}.txt"])
            self.assertIn(f"edit-{i}", H.read(results[i].log + ".out.txt") + json.dumps(rows))


ACP_LLM = r'''
import json, os, sys
import helpers_capture as C
C.load_plugin_package()
st = sys.modules["rt_plugin_under_test.state"]
cap = sys.modules["rt_plugin_under_test.capture"]
r = st.Rehearsal(st.new_id(), os.environ["CAP_WS"], task="t", mode="run")
r.create()
st.arm_process(r)
print(json.dumps({"rid": r.id, "meta_path": r.meta_path}), flush=True)
called = []
def next_call(req):
    called.append(req)
    open(os.environ["CAP_MARK"], "w").write("provider was called")
    return {"ok": True}
cap.llm_execution(next_call=next_call, request={"messages": []}, base_url="acp://copilot", api_mode="chat_completions")
print(json.dumps({"returned": True, "called": len(called)}), flush=True)
'''


class CapAcp(unittest.TestCase):
    def test_refused_by_provider_name_before_the_model(self):
        ws = H.make_ws("cap_acp_name", {"a.txt": "a\n"})
        ids0 = H.rehearsal_ids()
        stub0 = len(H.lines(H.STUB_LOG))
        mark = H.script("cap_acp", [[call("write_file", path="b.txt", content="b\n")]])
        p = H.hermes([H.CLI, "run", f"{mark} do it", "-m", "scripted", "--provider", "copilot-acp"], cwd=ws,
                     label="cap_acp_name")
        C.dump("acp_name", {"rc": p.returncode, "stderr": p.stderr[-1500:]})
        self.assertEqual(p.returncode, 3, p.stderr[-1500:])
        self.assertIn("runs tools outside Hermes", p.stderr)
        self.assertEqual(H.rehearsal_ids(), ids0, "a rehearsal was armed although it was refused")
        self.assertEqual(len(H.lines(H.STUB_LOG)), stub0, "the model was called")
        self.assertFalse(os.path.exists(os.path.join(ws, "b.txt")))

    def test_refused_at_the_first_model_call(self):
        """A provider that only shows it is ACP at call time (base_url acp://…): the process exits before the
        provider is called and the rehearsal is marked refused."""
        home = H.path("homes", "acp")
        os.makedirs(home, exist_ok=True)
        ws = H.make_ws("cap_acp_call", {"a.txt": "a\n"})
        mark = H.path("logs", "acp_provider_called.txt")
        r = subprocess.run([H.PY, "-B", "-c", ACP_LLM], cwd=os.path.join(H.REPO, "tests"), capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, HERMES_HOME=home, CAP_WS=ws, CAP_MARK=mark,
                                                           HERMES_TEST_HOME=H.HOME))
        first = json.loads(r.stdout.splitlines()[0])
        with open(first["meta_path"]) as f:
            meta = json.load(f)
        C.dump("acp_call", {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr, "meta_status": meta.get("status")})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertNotIn("returned", r.stdout, "llm_execution returned instead of stopping the process")
        self.assertFalse(os.path.exists(mark), "the ACP provider was called")
        self.assertIn("refusing to continue", r.stderr)
        self.assertEqual(meta.get("status"), "refused")
        self.assertIn("acp://copilot", meta.get("refused", ""))


if __name__ == "__main__":
    unittest.main()
