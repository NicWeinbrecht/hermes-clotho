"""Forks through Hermes' real agent loop, with scripted tool calls (no model, nothing leaves the machine).

ForkThree      3 timelines (variants with different scripted tool calls) at once: 0 sink requests and an
               unchanged workspace during the fork, one rehearsal per timeline with its own ledger and
               clone, the comparison names the differing effects in words and keeps lines within 58
               columns, applying one timeline lands exactly its effects (tree hash of its clone, its sink
               body) with 0 model calls, and a second timeline of the same fork is refused
ForkCap        a timeline over its model-call cap gets a synthetic stop: the provider sees only the calls
               under the cap, the ledger holds only their effects, the fork record says capped
ForkModels     --models with one timeline that cannot start: it is named as failed, the others run, the
               comparison of identical timelines says there are no differences; two Claude-plan timelines
               are refused before anything starts
ForkWall       wall-clock of a 3-timeline fork against a 1-timeline fork of the same task (target <= 1.5x)
"""
import json
import os
import shutil
import unittest

import harness as H
import helpers_capture as C
import helpers_fork as F
import names
from harness import call

PLAN = "claude-subscription-directsdk-experimental"


def setUpModule():
    C.start_servers()


def tearDownModule():
    C.stop_servers()


def _stub():
    return f"scripted@{H.STUB_PROVIDER}"


class ForkThree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tag = "f3"
        files = {"notes.txt": "v0\n", "keep/a.txt": "a\n", "keep/b.txt": "b\n", "keep/c.txt": "c\n",
                 "readme.md": "hello\n"}
        cls.ws = H.make_ws("fork_three", files)
        cls.golden = H.make_ws("fork_three_golden", files)
        cls.before = H.tree(cls.ws)
        url = f"http://127.0.0.1:{H.SINK_PORT}/fork/{cls.tag}"
        cls.marks = {
            "tidy": H.script("fork3_tidy", [
                [call("read_file", path="notes.txt")],
                [call("write_file", path="notes.txt", content="tidied by A\n")],
                [call("terminal", command=f"curl -s -X POST {url}/tidy -d 'summary from tidy'")],
            ], "tidy done"),
            "space": H.script("fork3_space", [
                [call("terminal", command="ls keep")],
                [call("terminal", command="rm keep/a.txt keep/b.txt keep/c.txt")],
                [call("terminal", command=f"curl -s -X POST {url}/space -d 'summary from space'")],
            ], "space done"),
            "archive": H.script("fork3_archive", [
                [call("terminal", command="mkdir -p Archive && mv keep/a.txt keep/b.txt keep/c.txt Archive/")],
                [call("read_file", path="notes.txt")],
                [call("write_file", path="notes.txt", content="archived by C\n")],
            ], "archive done"),
        }
        vf = F.variants_file("fork3", [{"name": n, "instructions": m} for n, m in cls.marks.items()],
                             model=_stub())
        cls.sink0 = len(H.sink_requests(f"/fork/{cls.tag}"))
        cls.p = F.fork([f"Clean up this folder ({cls.tag}).", "--variants", vf], cls.ws, "fork3")
        cls.after_fork = H.tree(cls.ws)
        cls.sink_after_fork = H.sink_requests(f"/fork/{cls.tag}")
        cls.rec = F.record(cls.p.fid) if cls.p.fid else None
        F.dump("three", {"rc": cls.p.returncode, "stderr": cls.p.stderr[-3000:], "rec": cls.rec})

    def test_1_fork_runs_three_isolated_timelines(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr[-2000:])
        self.assertEqual(self.sink_after_fork[self.sink0:], [], "a request escaped during the fork")
        self.assertEqual(self.after_fork, self.before, "the workspace changed during the fork")
        tls = {t["name"]: t for t in self.rec["timelines"]}
        self.assertEqual(sorted(tls), ["archive", "space", "tidy"])
        self.assertTrue(all(t["status"] == "finished" for t in tls.values()), tls)
        rids = [t["rid"] for t in tls.values()]
        self.assertEqual(len(set(rids)), 3, rids)
        self.assertEqual(self.rec["mode"], "at once")
        for name, t in tls.items():
            m = H.meta(t["rid"])
            self.assertEqual((m["fork_id"], m["timeline"]), (self.p.fid, name))
            self.assertEqual(m["workspace"], self.ws)
            self.assertNotEqual(H.rdir(t["rid"]), self.ws)
        # each ledger holds only its own timeline's effects
        rows = {n: H.ledger(t["rid"]) for n, t in tls.items()}
        urls = {n: [r["target"]["url"] for r in rs if r["class"] == "http"] for n, rs in rows.items()}
        self.assertEqual(urls["tidy"], [f"http://127.0.0.1:{H.SINK_PORT}/fork/{self.tag}/tidy"])
        self.assertEqual(urls["space"], [f"http://127.0.0.1:{H.SINK_PORT}/fork/{self.tag}/space"])
        self.assertEqual(urls["archive"], [])
        deletes = {n: sorted(r["target"]["path"] for r in rs if r["class"] == "file" and r["change"] == "deleted")
                   for n, rs in rows.items()}
        self.assertEqual(deletes["space"], ["keep/a.txt", "keep/b.txt", "keep/c.txt"])
        self.assertEqual(deletes["tidy"], [])
        self.assertEqual(deletes["archive"], [])
        moves = sorted(r["target"]["path"] for r in rows["archive"] if r["class"] == "file" and r["change"] == "renamed")
        self.assertEqual(moves, ["Archive/a.txt", "Archive/b.txt", "Archive/c.txt"])
        self.assertEqual(H.read(os.path.join(H.rdir(tls["tidy"]["rid"]), "work", "notes.txt")), "tidied by A\n")
        self.assertEqual(H.read(os.path.join(H.rdir(tls["archive"]["rid"]), "work", "notes.txt")), "archived by C\n")
        self.assertFalse(os.path.exists(os.path.join(H.rdir(tls["tidy"]["rid"]), "work", "Archive")))

    def test_2_comparison_names_the_differences(self):
        c = F.compare(self.p.fid, self.ws, "fork3_compare_json", "--json")
        self.assertEqual(c.returncode, 0, c.stderr[-1500:])
        texts = [d["text"] for d in c.data["differences"]]
        F.dump("three_diffs", texts)
        self.assertIn("only in space: delete 3 files in keep/: a.txt, b.txt, c.txt", texts)
        self.assertIn("only in archive: move 3 files into Archive/", texts)
        self.assertIn(f"only in tidy: POST http://127.0.0.1:{H.SINK_PORT}/fork/{self.tag}/tidy", texts)
        self.assertIn(f"only in space: POST http://127.0.0.1:{H.SINK_PORT}/fork/{self.tag}/space", texts)
        self.assertIn("not in space: modify notes.txt, different content", texts)
        self.assertLess(c.data["render_seconds"], 1.0)
        t = F.compare(self.p.fid, self.ws, "fork3_compare_text")
        self.assertEqual(t.returncode, 0, t.stderr[-1500:])
        self.assertIn("only in space: delete 3 files in keep/: a.txt, b.txt, c.txt", " ".join(t.stdout.split()))
        self.assertNotIn("\033[", t.stdout, "colour codes although NO_COLOR is set and stdout is not a TTY")
        long = [ln for ln in t.stdout.splitlines() if len(ln) > 58]
        self.assertEqual(long, [], "lines over 58 columns")
        for name in ("tidy", "space", "archive"):
            self.assertIn(f"apply {self.p.fid}/{name}", t.stdout)
        self.assertIn("only in space: delete 3 files in keep/: a.txt, b.txt, c.txt", " ".join(self.p.stdout.split()),
                      "fork did not print the comparison")

    def test_3_apply_one_timeline_lands_exactly_its_effects(self):
        tls = {t["name"]: t for t in self.rec["timelines"]}
        stub0 = len(H.lines(H.STUB_LOG))
        a = F.apply(f"{self.p.fid}/space", self.ws, "fork3_apply_space")
        F.dump("three_apply", {"rc": a.returncode, "receipt": a.receipt, "stderr": a.stderr[-2000:]})
        self.assertEqual(a.returncode, 0, a.stderr[-2000:])
        self.assertEqual(a.receipt["model_calls"], 0)
        self.assertEqual(len(H.lines(H.STUB_LOG)), stub0, "apply called the model")
        self.assertEqual(a.receipt["fork"]["timeline"], "space")
        # the workspace is now exactly the space timeline's would-be state, and nothing of the others
        self.assertEqual(H.tree(self.ws), H.tree(os.path.join(H.rdir(tls["space"]["rid"]), "work")))
        self.assertNotEqual(H.tree(self.ws), H.tree(os.path.join(H.rdir(tls["archive"]["rid"]), "work")))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "Archive")))
        self.assertEqual(H.read(os.path.join(self.ws, "notes.txt")), "v0\n")
        bodies = [(r["path"], r["body"]) for r in H.sink_requests(f"/fork/{self.tag}")[self.sink0:]]
        self.assertEqual(bodies, [(f"/fork/{self.tag}/space", "summary from space")])
        # a second timeline of the same fork is refused: it would mix two futures
        b = F.apply(f"{self.p.fid}/tidy", self.ws, "fork3_apply_tidy_refused")
        self.assertEqual(b.returncode, 3, b.stdout + b.stderr[-1500:])
        self.assertIn("already applied", b.stderr)
        self.assertEqual([r["path"] for r in H.sink_requests(f"/fork/{self.tag}")[self.sink0:]],
                         [f"/fork/{self.tag}/space"])
        rec = F.record(self.p.fid)
        self.assertEqual([x["timeline"] for x in rec["applied"]], ["space"])


class ForkBareApply(unittest.TestCase):
    """`hermes clotho apply` with no id never picks a timeline. Seen: after a fork, a bare `apply --dry-run`
    took the timeline whose rehearsal id sorted last (same-second ids sort by their random suffix)."""

    def test_1_bare_apply_after_a_fork_names_the_timelines_and_changes_nothing(self):
        tag = "fbare"
        ws = H.make_ws("fork_bare", {"notes.txt": "v0\n", "old.txt": "old\n"})
        url = f"http://127.0.0.1:{H.SINK_PORT}/fork/{tag}"
        marks = {
            "post": H.script("forkbare_post", [
                [call("write_file", path="notes.txt", content="by post\n")],
                [call("terminal", command=f"curl -s -X POST {url}/post -d 'from post'")],
            ], "post done"),
            "delete": H.script("forkbare_delete", [
                [call("terminal", command="rm old.txt")],
                [call("terminal", command=f"curl -s -X POST {url}/delete -d 'from delete'")],
            ], "delete done"),
        }
        vf = F.variants_file("forkbare", [{"name": n, "instructions": m} for n, m in marks.items()], model=_stub())
        p = F.fork([f"Tidy this folder ({tag}).", "--variants", vf], ws, "forkbare")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        rec = F.record(p.fid)
        tree0, sink0, stub0 = H.tree(ws), len(H.sink_requests(f"/fork/{tag}")), len(H.lines(H.STUB_LOG))
        for flags in ([], ["--dry-run"], ["--json"]):
            a = H.hermes([H.CLI, "apply", *flags], cwd=ws, label="forkbare_apply" + "".join(flags).replace("-", "_"))
            F.dump("bare_apply" + "".join(flags), {"rc": a.returncode, "stdout": a.stdout, "stderr": a.stderr[-2000:]})
            self.assertEqual(a.returncode, 3, a.stdout + a.stderr[-1500:])
            self.assertEqual(a.stdout.strip(), "")
            joined = " ".join(a.stderr.split()).replace("\\ ", "")
            for name in marks:
                self.assertIn(f"hermes {H.CLI} apply {p.fid}/{name}", joined)
            self.assertIn("nothing was changed", a.stderr)
        self.assertEqual(H.tree(ws), tree0, "the workspace changed")
        self.assertEqual(len(H.sink_requests(f"/fork/{tag}")), sink0, "the sink got a request")
        self.assertEqual(len(H.lines(H.STUB_LOG)), stub0, "apply called the model")
        self.assertFalse(F.record(p.fid).get("applied"))
        for t in rec["timelines"]:
            rd = os.path.join(H.rdir(t["rid"]), "receipts")
            self.assertFalse(os.path.isdir(rd) and os.listdir(rd), f"a receipt was written for {t['name']}")

    def test_2_a_plain_rehearsal_after_the_fork_applies_as_before(self):
        tag = "fbare_plain"
        ws = H.make_ws("fork_bare_plain", {"todo.md": "- [ ] a\n"})
        url = f"http://127.0.0.1:{H.SINK_PORT}/fork/{tag}"
        mark = H.script("forkbare_plain", [
            [call("write_file", path="done.txt", content="done\n")],
            [call("terminal", command=f"curl -s -X POST {url} -d 'plain'")],
        ], "done")
        r = H.rehearse(mark, ws, "forkbare_plain_run")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        bare = H.hermes([H.CLI, "apply", "--dry-run"], cwd=ws, label="forkbare_plain_dry")
        named = H.hermes([H.CLI, "apply", r.rid, "--dry-run"], cwd=ws, label="forkbare_plain_dry_id")
        self.assertEqual((bare.returncode, bare.stdout), (named.returncode, named.stdout))
        self.assertEqual(bare.returncode, 0, bare.stderr[-1500:])
        sink0 = len(H.sink_requests(f"/fork/{tag}"))
        a = H.hermes([H.CLI, "apply", "--json"], cwd=ws, label="forkbare_plain_apply")
        self.assertEqual(a.returncode, 0, a.stdout[-1500:] + a.stderr[-1500:])
        self.assertEqual(json.loads(a.stdout)["model_calls"], 0)
        self.assertEqual(H.read(os.path.join(ws, "done.txt")), "done\n")
        self.assertEqual([x["body"] for x in H.sink_requests(f"/fork/{tag}")[sink0:]], ["plain"])


class ForkApplyEmptyFolder(unittest.TestCase):
    def test_applying_a_timeline_that_creates_an_empty_folder(self):
        """Seen live (deepseek-v4-flash made an empty Data/ folder): Apply stopped at that effect with
        "TypeError: join() argument must be str, bytes, or os.PathLike object, not 'NoneType'" after applying
        the effects before it. A created folder has no blob to copy."""
        ws = H.make_ws("fork_emptydir", {"a.txt": "a\n"})
        mark = H.script("fork_emptydir", [[call("terminal", command="mkdir Data && mkdir -p Docs && mv a.txt Docs/")]],
                        "done")
        p = F.fork([f"{mark} sort", "--models", f"solo={_stub()}"], ws, "fork_emptydir")
        rid = F.record(p.fid)["timelines"][0]["rid"]
        a = F.apply(f"{p.fid}/solo", ws, "fork_emptydir_apply")
        F.dump("emptydir", {"fork_rc": p.returncode, "rc": a.returncode, "receipt": a.receipt,
                            "stderr": a.stderr[-1500:]})
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertEqual(a.returncode, 0, json.dumps(a.receipt, default=str)[-1500:] if a.receipt else a.stderr)
        self.assertEqual(H.tree(ws), H.tree(os.path.join(H.rdir(rid), "work")))


class ForkCap(unittest.TestCase):
    def test_over_the_cap_gets_a_synthetic_stop(self):
        ws = H.make_ws("fork_cap", {"a.txt": "a\n"})
        mark = H.script("fork_cap", [[call("write_file", path=f"step_{k}.txt", content=f"{k}\n")] for k in range(1, 6)],
                        "all five steps done")
        n0 = F.tools_requests()
        p = F.fork([f"{mark} write the steps", "--models", f"capped={_stub()}", "--max-calls", "2"], ws, "fork_cap")
        n1 = F.tools_requests()
        rec = F.record(p.fid)
        t = rec["timelines"][0]
        F.dump("cap", {"rc": p.returncode, "rec": rec, "stderr": p.stderr[-2000:], "tools_requests": n1 - n0})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual(n1 - n0, 2, "the provider answered more model calls than the cap")
        self.assertEqual(t["status"], "capped")
        self.assertEqual(t["model_calls"], 2)
        self.assertGreaterEqual(t["capped_calls"], 1)
        files = sorted(r["target"]["path"] for r in H.ledger(t["rid"]) if r["class"] == "file")
        self.assertEqual(files, ["step_1.txt", "step_2.txt"])
        self.assertIn("cap of 2 model calls", H.meta(t["rid"]).get("agent_final_text", ""))
        self.assertIn("stopped at its cap of 2 model calls", p.stdout)
        self.assertEqual(sorted(os.listdir(ws)), ["a.txt"])


class ForkModels(unittest.TestCase):
    def test_models_with_one_that_cannot_start(self):
        ws = H.make_ws("fork_models", {"a.txt": "a\n"})
        mark = H.script("fork_models", [[call("write_file", path="b.txt", content="b\n")]], "done")
        p = F.fork([f"{mark} write b", "--models", f"one={_stub()},two={_stub()},broken=scripted@no-such-provider"],
                   ws, "fork_models")
        rec = F.record(p.fid)
        F.dump("models", {"rc": p.returncode, "rec": rec, "stdout": p.stdout, "stderr": p.stderr[-3000:]})
        tls = {t["name"]: t for t in rec["timelines"]}
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertEqual((tls["one"]["status"], tls["two"]["status"]), ("finished", "finished"))
        self.assertEqual(tls["broken"]["status"], "failed")
        self.assertIn("Unknown provider 'no-such-provider'", tls["broken"]["reason"])
        self.assertIn("failed: hermes -z: agent failed: Unknown", p.stdout)
        self.assertIn("not compared: broken (failed)", p.stdout)
        self.assertIn("no differences: the 2 ledgers hold the same effects", p.stdout)
        self.assertNotIn(f"apply {p.fid}/broken", p.stdout)
        self.assertEqual(sorted(os.listdir(ws)), ["a.txt"])

    def test_two_plan_timelines_are_refused(self):
        ws = H.make_ws("fork_two_plan", {"a.txt": "a\n"})
        stub0 = len(H.lines(H.STUB_LOG))
        ids0 = H.rehearsal_ids()
        p = F.fork(["say hi", "--models", f"c1=sonnet@{PLAN},c2=haiku@{PLAN}"], ws, "fork_two_plan")
        self.assertEqual(p.returncode, 2, p.stderr[-1500:])
        self.assertIn("at most 1 timeline", p.stderr)
        self.assertIsNone(p.fid)
        self.assertEqual(H.rehearsal_ids(), ids0)
        self.assertEqual(len(H.lines(H.STUB_LOG)), stub0)


class ForkCollide(unittest.TestCase):
    def test_collided_timelines_run_again_one_after_another(self):
        """A timeline that fails because it got in another's way (here a test seam makes its first start fail
        with 'database is locked') is run again after the others, and the output says so."""
        ws = H.make_ws("fork_collide", {"a.txt": "a\n"})
        mark = H.script("fork_collide", [[call("write_file", path="b.txt", content="b\n")]], "done")
        p = F.fork([f"{mark} write b", "--models", f"first={_stub()},second={_stub()}"], ws, "fork_collide",
                   **{names.env("TEST_COLLIDE"): "second"})
        rec = F.record(p.fid)
        F.dump("collide", {"rc": p.returncode, "rec": rec, "stdout": p.stdout, "stderr": p.stderr[-2000:]})
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        tls = {t["name"]: t for t in rec["timelines"]}
        self.assertEqual(rec["mode"], "at once, then one after another")
        self.assertEqual([c["timeline"] for c in rec["collisions"]], ["second"])
        self.assertIn("database is locked", rec["collisions"][0]["reason"])
        self.assertEqual((tls["first"]["status"], tls["second"]["status"]), ("finished", "finished"))
        self.assertEqual(tls["second"].get("rerun"), "one after another")
        self.assertNotIn("rerun", tls["first"])
        self.assertIn("second collided when run at once", " ".join(p.stdout.split()))
        self.assertIn("ran again after the others", " ".join(p.stdout.split()))
        self.assertIn("running them again one after another", p.stderr)
        self.assertTrue(os.path.exists(os.path.join(F.forks_root(), p.fid, "second.err.txt.collided")))


class ForkWall(unittest.TestCase):
    def test_three_timelines_against_one(self):
        """Each step waits 3 s in a sandboxed `sleep`, standing in for model latency (real model turns take
        tens of seconds, so Hermes' start-up does not dominate a real fork the way it would a bare script)."""
        ws = H.make_ws("fork_wall", {"a.txt": "a\n"})
        mark = H.script("fork_wall", [[call("terminal", command="sleep 3")], [call("terminal", command="sleep 3")],
                                      [call("write_file", path="out.txt", content="x\n")]], "done")
        one = F.fork([f"{mark} work", "--models", f"solo={_stub()}"], ws, "fork_wall_1")
        three = F.fork([f"{mark} work", "--models", f"a={_stub()},b={_stub()},c={_stub()}"], ws, "fork_wall_3")
        r1, r3 = F.record(one.fid), F.record(three.fid)
        ratio = r3["wall_seconds"] / r1["wall_seconds"]
        F.dump("wall", {"one": r1["wall_seconds"], "three": r3["wall_seconds"], "ratio": round(ratio, 3),
                        "three_timelines": [t["seconds"] for t in r3["timelines"]],
                        "one_timeline": [t["seconds"] for t in r1["timelines"]]})
        self.assertEqual((one.returncode, three.returncode), (0, 0), three.stderr[-1500:])
        self.assertTrue(all(t["status"] == "finished" for t in r3["timelines"]), r3["timelines"])
        self.assertLessEqual(ratio, 1.5, f"3 timelines took {ratio:.2f}x the wall-clock of 1")


if __name__ == "__main__":
    unittest.main()
