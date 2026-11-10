"""Calls that reach the plugin through Hermes' tool_call bridge, and parallel tool calls in one turn.

CapParallel     two write_file calls in one model turn (Hermes runs them as a parallel batch) finish as fast as
                with the plugin off, and both are in the ledger
CapBridge       tool_call {name, arguments} and the batch form {calls: [...]} are recorded under the real tool,
                classified and ticked as that tool would be; tool_search / tool_describe run for real (catalog
                reads), never as effects
"""
import json
import re
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


class CapParallel(unittest.TestCase):
    def test_two_writes_in_one_turn(self):
        """Hermes runs the calls of one turn on parallel workers and starts them in call order (its start-order
        gate gives up after 120 s). Which worker reaches the plugin first varies, so three turns are tried."""
        ws = H.make_ws("cap_par", {"keep.txt": "keep\n", "todo.md": "- [ ] summary\n"})
        before = H.tree(ws)
        mark = H.script("cap_par", [
            [call("write_file", path="a.md", content="a\n"), call("write_file", path="b.md", content="b\n")],
            [call("write_file", path="c.md", content="c\n"), call("write_file", path="d.md", content="d\n")],
            [call("read_file", path="todo.md")],
            [call("write_file", path="e.md", content="e\n"),
             call("patch", path="todo.md", old_string="- [ ]", new_string="- [x]")],
        ], "Wrote them all.")
        t0 = time.monotonic()
        p = H.rehearse(mark, ws, "cap_par")
        wall = time.monotonic() - t0
        rows = H.ledger(p.rid)
        calls = C.middleware_calls(p.rid)
        C.dump("parallel", {"rid": p.rid, "wall": round(wall, 2), "seconds": H.meta(p.rid).get("seconds"),
                            "rows": [(r["id"], r["class"], r["summary"]) for r in rows], "calls": calls})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(H.tree(ws), before)
        self.assertEqual(sorted(r["target"]["path"] for r in rows if r["class"] == "file"),
                         ["a.md", "b.md", "c.md", "d.md", "e.md", "todo.md"])
        self.assertLess(wall, 40, f"three parallel turns took {wall:.1f} s")
        self.assertEqual(len({c["seq"] for c in calls}), len(calls), f"two calls share one seq: {calls}")
        work = os.path.join(H.rdir(p.rid), "work")
        self.assertEqual(H.read(os.path.join(work, "todo.md")), "- [x] summary\n")


def _bridge(tool, **arguments):
    return call("tool_call", name=tool, arguments=arguments)


class CapBridge(unittest.TestCase):
    def _run(self, label, steps, final="Done.", job=None):
        ws = H.make_ws(label, {"a.txt": "alpha\n"})
        before = H.tree(ws)
        mark = H.script(label, steps, final)
        p = H.rehearse(mark, ws, label)
        rows = H.ledger(p.rid)
        calls = C.middleware_calls(p.rid)
        C.dump(label, {"rid": p.rid, "rows": rows, "calls": calls, "out": p.stdout[-3000:]})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(H.tree(ws), before)
        if job:
            jobs = os.path.join(H.HOME, "cron", "jobs.json")
            self.assertNotIn(job, H.read(jobs) if os.path.exists(jobs) else "", "the rehearsal created the cron job")
        return p, rows, calls

    def test_single_form_cron_job(self):
        name = f"digest-{time.time_ns() % 10**6}"
        p, rows, calls = self._run("cap_br_cron", [[_bridge("cronjob_manage", action="create", name=name,
                                                            schedule="every monday 9am", prompt="digest",
                                                            deliver="local", paused=True)]], job=name)
        self.assertEqual(len(rows), 1, [r["summary"] for r in rows])
        r = rows[0]
        self.assertEqual(r["tool"], "cronjob_manage", r)
        self.assertEqual(r["class"], "state", r)
        self.assertTrue(r["default_selected"], r)
        self.assertEqual(r["args"].get("name"), name)
        self.assertNotIn("UNKNOWN TOOL", r["badges"])
        self.assertEqual(r["source"].get("bridge"), "tool_call")

    def test_batch_form_cron_job_applies(self):
        """The batch form of a tool outside the session's toolsets (the case Hermes leaves wrapped), then a
        default Apply: the cron job exists afterwards, created by the real tool."""
        name = f"digestb-{time.time_ns() % 10**6}"
        p, rows, calls = self._run("cap_br_cronb", [[call("tool_call", calls=[{"name": "cronjob_manage", "arguments": {
            "action": "create", "name": name, "schedule": "every monday 9am", "prompt": "digest", "deliver": "local",
            "paused": True}}])]], job=name)
        self.assertEqual([(r["tool"], r["class"], r["default_selected"]) for r in rows],
                         [("cronjob_manage", "state", True)], [r["summary"] for r in rows])
        self.assertIn(name, rows[0]["summary"])
        jobs = os.path.join(H.HOME, "cron", "jobs.json")
        a = H.apply(p.rid, H.meta(p.rid)["workspace"], "cap_br_cronb_apply")
        self.assertEqual(a.returncode, 0, a.stdout[-1500:] + a.stderr[-1500:])
        self.assertIn(name, H.read(jobs), "Apply did not create the cron job")

    def test_batch_form_plugin_sends(self):
        p, rows, calls = self._run("cap_br_batch", [
            [call("tool_call", calls=[{"name": "testkit_send", "arguments": {"to": "#team", "text": "br-batch-1"}}])],
            [call("tool_call", calls=[{"name": "testkit_send", "arguments": {"to": "#team", "text": "br-batch-2"}},
                                      {"name": "testkit_send", "arguments": {"to": "#bots", "text": "br-batch-3"}}])],
        ])
        self.assertEqual(H.sink_with("br-batch"), [], "a bridged send ran for real")
        sends = [r for r in rows if r["tool"] == "testkit_send"]
        self.assertEqual(sorted(r["args"]["text"] for r in sends), ["br-batch-1", "br-batch-2", "br-batch-3"],
                         [(r["tool"], r["summary"]) for r in rows])
        for r in sends:
            self.assertEqual(r["class"], "outbound")
            self.assertTrue(r["default_selected"], r)
        self.assertFalse([r for r in rows if r["tool"] == "tool_call"], [r["summary"] for r in rows])

    def test_single_form_plugin_tool_is_labelled(self):
        """Hermes unwraps a single-form tool_call to a tool the session may reach BEFORE the middleware sees it,
        so the plugin only gets the real name; the row must still say it came through the bridge (schema:
        source.bridge). A direct call of a listed tool must not get the label."""
        p, rows, calls = self._run("cap_br_single", [
            [_bridge("testkit_send", to="#team", text="br-single-1")],
            [call("tool_call", calls=[{"name": "testkit_send", "arguments": {"to": "#ops", "text": "br-single-2"}}])],
            [call("write_file", path="direct.txt", content="d\n")],
        ])
        self.assertEqual(H.sink_with("br-single"), [], "a bridged send ran for real")
        sends = sorted((r for r in rows if r["tool"] == "testkit_send"), key=lambda r: r["args"]["text"])
        self.assertEqual([r["args"]["text"] for r in sends], ["br-single-1", "br-single-2"],
                         [(r["tool"], r["summary"]) for r in rows])
        for r in sends:
            self.assertEqual(r["source"].get("bridge"), "tool_call", r["source"])
        direct = [r for r in rows if r["tool"] == "write_file"]
        self.assertEqual(len(direct), 1, [r["summary"] for r in rows])
        self.assertNotIn("bridge", direct[0]["source"])

    def test_split_batch_entries_are_labelled(self):
        """Hermes splits a tool_call batch that names a local tool into single calls (a new id for each entry
        after the first) and unwraps each before the middleware sees it. Seen in the check: those rows had no
        source.bridge, both for a batch of two local sends and for a local send batched with a connector call.
        A direct call after them stays unlabelled. (testkit_send itself cannot be called directly: Hermes
        defers plugin tools behind tool_search, and refuses the direct call without a row.)"""
        p, rows, calls = self._run("cap_br_split", [
            [call("tool_call", calls=[{"name": "testkit_send", "arguments": {"to": "#team", "text": "br-split-1"}},
                                      {"name": "testkit_send", "arguments": {"to": "#ops", "text": "br-split-2"}}])],
            [call("tool_call", calls=[{"name": "testkit_send", "arguments": {"to": "#team", "text": "br-split-3"}},
                                      {"name": "connectors__fake__push", "arguments": {"text": "br-split-4"}}])],
            [call("write_file", path="direct.txt", content="d\n")],
        ])
        self.assertEqual(H.sink_with("br-split"), [], "a bridged send ran for real")
        sends = {r["args"]["text"]: r for r in rows if r["tool"] == "testkit_send"}
        self.assertEqual(sorted(sends), ["br-split-1", "br-split-2", "br-split-3"],
                         [(r["tool"], r["summary"]) for r in rows])
        for t in ("br-split-1", "br-split-2", "br-split-3"):
            self.assertEqual(sends[t]["source"].get("bridge"), "tool_call", (t, sends[t]["source"]))
        direct = [r for r in rows if r["tool"] == "write_file"]
        self.assertEqual(len(direct), 1, [r["summary"] for r in rows])
        self.assertNotIn("bridge", direct[0]["source"])

    def test_catalog_reads_run(self):
        p, rows, calls = self._run("cap_br_catalog", [[call("tool_search", query="send a message")],
                                                      [call("tool_describe", names=["testkit_send"])]])
        self.assertEqual(rows, [], [r["summary"] for r in rows])
        dec = {c["tool"]: c["decision"] for c in calls}
        self.assertEqual(dec.get("tool_search"), "run", calls)
        self.assertEqual(dec.get("tool_describe"), "run", calls)


class CapCatalogParity(unittest.TestCase):
    """Seen in the measurement (EVAL.md N1): in a rehearsal tool_search reported total_available 8 and
    tool_describe said "'cronjob_manage' is not available in this execution context"; with the plugin off it
    was 9 and cronjob_manage matched. The same scripted turns, `hermes -z` against `clotho run`: the
    model must be offered the same tools and the catalog must answer the same."""

    def _turns(self, runner, label):
        ws = H.make_ws(label, {"a.txt": "alpha\n"})
        mark = H.script(label, [[call("tool_search", queries=["cron job schedule"])],
                                [call("tool_describe", names=["cronjob_manage"])]], "Done.")
        n0 = len(H.lines(H.STUB_LOG))
        p = runner(mark, ws, label)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        reqs = [x for x in H.lines(H.STUB_LOG)[n0:] if x.get("method") == "POST" and x.get("has_tools")]
        offered = next((x["tools"] for x in reqs if x.get("tools")), None)
        results = [x["tool_results"] for x in reqs if x.get("tool_results")]
        return offered, results

    def test_same_catalog_as_a_plain_run(self):
        try:
            plain_tools, plain = self._turns(H.plain_run, "cap_cat_plain")
        finally:  # a plain -z leaves a gateway running, whose kanban dispatcher touches the home meanwhile
            H.hermes(["gateway", "stop"], cwd=H.root(), label="cap_cat_gateway_stop")
        rt_tools, rt = self._turns(H.rehearse, "cap_cat_rt")
        C.dump("catalog_parity", {"plain_tools": plain_tools, "rt_tools": rt_tools, "plain": plain, "rt": rt})
        self.assertEqual(len(plain), 2, plain)
        self.assertEqual(len(rt), 2, rt)

        def total(res):
            m = re.search(r'"total_available"\s*:\s*(\d+)', res[0][0])
            return int(m.group(1)) if m else None
        self.assertIsNotNone(total(plain), plain[0][0][:500])
        self.assertIn("cronjob_manage", plain[0][0], "the plain run's catalog has no cronjob_manage (test premise)")
        self.assertEqual(total(rt), total(plain), (rt[0][0][:600], plain[0][0][:600]))
        self.assertIn("cronjob_manage", rt[0][0])
        self.assertNotIn("not available in this execution context", rt[1][0])
        self.assertEqual(rt_tools, plain_tools)


if __name__ == "__main__":
    unittest.main()
