"""Apply and report through a real Hermes run (scripted tool calls, no model), on the case that
left a half-applied state before: a file, a folder listing that names it, a POST that sends it with
--data-binary @file, a second POST, and one more file. Instruments: the local sink's request log and
a tree hash of the workspace."""
import json
import os
import re
import unittest

import ansi_contract as AC
import harness as H
from harness import call

SGR = re.compile(r"\x1b\[[0-9;]*m")


def setUpModule():
    H.require_home()
    H.start_servers()
    assert H.sink_selftest(), "the sink does not log requests: the instrument is broken"


def tearDownModule():
    H.stop_servers()


def _steps(port, tag):
    return [[call("write_file", path="count.txt", content="3\n")],
            [call("terminal", command="ls > listing.txt")],
            [call("terminal", command=f"curl -s -X POST http://127.0.0.1:{port}/w2a/{tag}/hook --data-binary @count.txt")],
            [call("terminal", command=f"curl -s -X POST http://127.0.0.1:{port}/w2a/{tag}/after -d after")],
            [call("write_file", path="hello.txt", content="hello\n")]]


class ApplyDependencies(unittest.TestCase):

    def _rehearse(self, tag):
        mark = H.script(f"w2a_{tag}", _steps(H.SINK_PORT, tag), "Counted, listed, posted.")
        ws = H.make_ws(f"w2a_{tag}", {"readme.txt": "read me\n"})
        p = H.rehearse(mark, ws, f"w2a_{tag}")
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        rows = H.ledger(p.rid)
        ids = {}
        for r in rows:
            s = r["summary"]
            if s.startswith("create count.txt"):
                ids["count"] = r["id"]
            elif s.startswith("create listing.txt"):
                ids["listing"] = r["id"]
            elif s.startswith("create hello.txt"):
                ids["hello"] = r["id"]
            elif r["class"] == "http" and "/hook" in s:
                ids["hook"] = r["id"]
            elif r["class"] == "http" and "/after" in s:
                ids["after"] = r["id"]
        self.assertEqual(set(ids), {"count", "listing", "hello", "hook", "after"}, [r["summary"] for r in rows])
        return ws, p.rid, ids

    def test_untick_a_dependency_stops_before_any_change(self):
        ws, rid, ids = self._rehearse("stop")
        before = H.tree(ws)
        a = H.apply(rid, ws, "w2a_stop", "--except", str(ids["count"]))
        self.assertEqual(a.returncode, 5, a.stdout[-1500:] + a.stderr[-800:])
        self.assertEqual(a.receipt["status"], "dependency")
        pairs = {(c["id"], c["needs"]): c["sure"] for c in a.receipt["conflicts"]}
        self.assertIs(pairs.get((ids["hook"], ids["count"])), True, a.receipt["conflicts"])
        self.assertNotIn((ids["listing"], ids["count"]), pairs, "a guess must not stop Apply (D34)")
        guesses = {(c["id"], c["needs"]): c["sure"] for c in a.receipt["guesses"]}
        self.assertIs(guesses.get((ids["listing"], ids["count"])), False, a.receipt)
        self.assertEqual(H.tree(ws), before, "a dependency stop must change nothing")
        self.assertEqual(H.sink_requests("/w2a/stop/"), [])
        text = H.hermes([H.CLI, "apply", rid, "--except", str(ids["count"])], cwd=ws, label="w2a_stop_text")
        self.assertEqual(text.returncode, 5)
        self.assertIn("--force", text.stdout)
        self.assertIn(f"needs #{ids['count']}", text.stdout)
        self.assertTrue(all(len(ln) <= 58 for ln in text.stdout.splitlines()), text.stdout)

    def test_force_failure_receipt_then_nothing_sent_twice(self):
        ws, rid, ids = self._rehearse("force")
        a = H.apply(rid, ws, "w2a_force", "--except", str(ids["count"]), "--force")
        self.assertEqual(a.returncode, 1, a.stdout[-1500:])
        rc = a.receipt
        self.assertEqual(rc["status"], "failed")
        self.assertEqual(rc["failed_ids"], [ids["hook"]])
        detail = {r["id"]: r for r in rc["results"]}[ids["hook"]]["detail"]
        self.assertIn("exit code 26", detail)
        self.assertEqual(rc["applied_ids"], [ids["listing"]])
        self.assertEqual(rc["not_attempted"], [ids["after"], ids["hello"]])
        self.assertFalse(os.path.exists(os.path.join(ws, "hello.txt")))
        self.assertEqual(H.sink_requests("/w2a/force/"), [])
        # now with every effect ticked: the dependency lands first, each POST is sent exactly once
        b = H.apply(rid, ws, "w2a_force_all")
        self.assertEqual(b.returncode, 0, b.stdout[-1500:])
        self.assertEqual(len(H.sink_requests("/w2a/force/hook")), 1)
        self.assertEqual(H.sink_requests("/w2a/force/hook")[0]["body"], "3\n")
        self.assertEqual(len(H.sink_requests("/w2a/force/after")), 1)
        self.assertEqual(H.read(os.path.join(ws, "hello.txt")), "hello\n")
        tree = H.tree(ws)
        c = H.apply(rid, ws, "w2a_force_again")
        self.assertEqual(c.returncode, 0)
        self.assertEqual(c.receipt["applied"], 0)
        self.assertEqual(len(H.sink_requests("/w2a/force/")), 2, "a repeated apply must send nothing again")
        self.assertEqual(H.tree(ws), tree)

    def test_report_width_colours_and_json(self):
        ws, rid, ids = self._rehearse("report")
        plain = H.hermes([H.CLI, "report", rid], cwd=ws, label="w2a_report_plain")
        self.assertEqual(plain.returncode, 0, plain.stderr[-800:])
        self.assertNotIn("\x1b", plain.stdout)
        self.assertEqual([ln for ln in plain.stdout.splitlines() if len(ln) > 58], [])
        self.assertIn("guess", plain.stdout)
        self.assertRegex(plain.stdout, r"5 effects · 2 irreversible")
        col = H.hermes([H.CLI, "report", rid], cwd=ws, label="w2a_report_colour", NO_COLOR="", FORCE_COLOR="1")
        self.assertIn(AC.BADGE, col.stdout)
        self.assertEqual(AC.check(self, col.stdout), [], "a report has no red")
        self.assertIn(AC.PLAIN_BADGE, plain.stdout)
        self.assertEqual([ln for ln in col.stdout.splitlines() if len(SGR.sub("", ln)) > 58], [])
        j = H.hermes([H.CLI, "report", rid, "--json"], cwd=ws, label="w2a_report_json")
        data = json.loads(j.stdout)
        self.assertEqual(set(data), {"rehearsal", "summary", "counts", "default_selection", "applied", "effects"})


if __name__ == "__main__":
    unittest.main()
