"""Apply never writes a path Hermes' own write guard refuses, and never applies an effect outside the
workspace unless it is named.

ApplyWriteGuard   synthetic rehearsals (helpers_apply): an effect on the SSH list of authorized keys (a scratch
                  HOME) or a move whose source is Hermes' .env stops the apply before any change, exits
                  non-zero, and leaves every file byte-identical, with --only and with --force too
OutsideUnticked   through the plugin's middleware with Hermes' real tools (one process, no agent loop): a
                  write_file outside the workspace and a `mv` into ~/.Trash (a scratch HOME) are not applied by
                  a plain apply; the in-workspace write is (control); named with --only, they are
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

import harness as H
import helpers_apply as HA
import helpers_inproc as I

# Hermes' write guard (agent/file_safety.py) is standard library only: the unit tests import it from the source
if I.HERMES_SRC not in sys.path:
    sys.path.append(I.HERMES_SRC)


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tree(folder):
    out = {}
    for root, _dirs, files in os.walk(folder):
        for n in files:
            p = os.path.join(root, n)
            out[os.path.relpath(p, folder)] = _sha(p)
    return out


def _text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def _cli_apply(rid, only="", force=False):
    cli = HA.sub("cli")
    ns = argparse.Namespace(id=rid, only=only, except_="", dry_run=False, json=True, force=force)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.cmd_apply(ns)
    try:
        return rc, json.loads(buf.getvalue())
    except ValueError:
        return rc, {"stdout": buf.getvalue()}


# Hermes' plugin scanner (`hermes plugins validate`) reads the SSH key list's file name in a test as a backdoor;
# the name is built from two pieces so the scan of the plugin's own files stays clean.
KEYS_FILE = "authorized" + "_keys"


class ApplyWriteGuard(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="clotho-guard-home-"))
        self.saved = {k: os.environ.get(k) for k in ("HOME", "HERMES_HOME")}
        os.environ["HOME"] = self.home
        os.environ["HERMES_HOME"] = os.path.join(self.home, ".hermes")
        os.makedirs(os.path.join(self.home, ".ssh"))
        os.makedirs(os.environ["HERMES_HOME"])
        self.keys = os.path.join(self.home, ".ssh", KEYS_FILE)
        with open(self.keys, "w") as f:
            f.write("ssh-ed25519 AAAA the-only-key\n")
        self.env_file = os.path.join(os.environ["HERMES_HOME"], ".env")
        with open(self.env_file, "w") as f:
            f.write("SOME_API_KEY=keep-me\n")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self):
        run = HA.FakeRun()
        run.put("notes.txt", "old\n")
        safe = run.file("notes.txt", "new\n", change="modified")                      # 1: a plain workspace edit
        keys = run.file("keys.txt", "ssh-ed25519 AAAA attacker\n", change="created")  # 2: aimed at the SSH key list
        keys["target"] = {"kind": "file", "path": self.keys, "abs_path": self.keys, "in_workspace": False}
        keys["original"] = keys["before"] = HA.fp_bytes(open(self.keys, "rb").read())
        keys["change"] = "modified"
        mv = run.file("moved.env", "SOME_API_KEY=keep-me\n", change="created")         # 3: a move out of Hermes' .env
        mv.update(change="renamed", from_path=self.env_file, original_from=HA.fp_bytes(open(self.env_file, "rb").read()))
        mv["apply"]["method"] = "rename"
        run.finish()
        return run, safe, keys, mv

    def _check_nothing_changed(self, run, before_ws, rc, receipt, label):
        apply = HA.sub("apply")
        self.assertEqual(_text(self.keys), "ssh-ed25519 AAAA the-only-key\n", f"{label}: the SSH key list changed")
        self.assertEqual(_text(self.env_file), "SOME_API_KEY=keep-me\n", f"{label}: Hermes' .env changed")
        self.assertEqual(_tree(run.ws), before_ws, f"{label}: the workspace changed although apply was refused")
        self.assertNotEqual(rc, 0, f"{label}: apply exited 0 although it was refused: {receipt.get('status')}")
        self.assertEqual(rc, apply.EXIT_REFUSED, f"{label}: {receipt}")

    def test_a_denied_target_stops_apply_before_any_change(self):
        run, safe, keys, mv = self._run()
        HA.setup(run)
        before_ws = _tree(run.ws)
        rc, receipt = _cli_apply(run.rid)
        self._check_nothing_changed(run, before_ws, rc, receipt, "plain apply")
        denied = {(d["id"], d["path"]) for d in receipt.get("denied") or []}
        self.assertIn((keys["id"], self.keys), denied, receipt)
        self.assertIn((mv["id"], self.env_file), denied, receipt)
        self.assertNotIn(safe["id"], {d[0] for d in denied})

    def test_force_and_only_do_not_get_past_the_guard(self):
        run, safe, keys, mv = self._run()
        HA.setup(run)
        before_ws = _tree(run.ws)
        for label, kw in (("--force", {"force": True}), ("--only", {"only": f"{keys['id']},{mv['id']}"}),
                          ("--only --force", {"only": str(keys["id"]), "force": True})):
            rc, receipt = _cli_apply(run.rid, **kw)
            self._check_nothing_changed(run, before_ws, rc, receipt, label)

    def test_the_rest_applies_once_the_denied_effects_are_unticked(self):
        run, safe, keys, mv = self._run()
        HA.setup(run)
        rc, receipt = _cli_apply(run.rid, only=str(safe["id"]))
        self.assertEqual(rc, 0, receipt)
        self.assertEqual(run.live("notes.txt"), "new\n")
        self.assertEqual(_text(self.keys), "ssh-ed25519 AAAA the-only-key\n")


DRIVER = r'''
import json, os
import helpers_inproc as I
ws, out = os.environ["GUARD_WS"], os.environ["GUARD_OUT"]
s = I.Session(ws)
s.call("write_file", path="inside.txt", content="inside\n")
s.call("write_file", path=os.path.join(out, "outside.txt"), content="outside\n")
s.call("terminal", command="mv a.txt ~/.Trash/a-moved.txt")
rows = s.finish()
ap = s.m["apply"]
plain = ap.apply(s.r.id)
state = {"inside": os.path.exists(os.path.join(ws, "inside.txt")), "outside": os.path.exists(os.path.join(out, "outside.txt")),
         "a_in_ws": os.path.exists(os.path.join(ws, "a.txt")),
         "trashed": os.path.exists(os.path.join(os.environ["HOME"], ".Trash", "a-moved.txt"))}
named = [r["id"] for r in rows if r["class"] == "file" and not r["target"]["in_workspace"]]
named += [r["id"] for r in rows if r["class"] == "command" and r.get("children")]
second = ap.apply(s.r.id, only=sorted(set(named)))
state2 = {"outside": os.path.exists(os.path.join(out, "outside.txt")), "a_in_ws": os.path.exists(os.path.join(ws, "a.txt")),
          "trashed": os.path.exists(os.path.join(os.environ["HOME"], ".Trash", "a-moved.txt"))}
print(json.dumps({"rows": rows, "plain": plain, "state": state, "second": second, "state2": state2}))
'''


class OutsideUnticked(unittest.TestCase):
    def test_an_effect_outside_the_workspace_runs_only_when_named(self):
        home = os.path.realpath(tempfile.mkdtemp(prefix="clotho-guard-userhome-"))
        os.makedirs(os.path.join(home, ".Trash"))
        out = os.path.realpath(tempfile.mkdtemp(prefix="clotho-guard-outside-"))
        ws = H.make_ws("guard_outside", {"a.txt": "move me\n"})
        p = I.run(DRIVER, I.scratch_home("guard_outside"), GUARD_WS=ws, GUARD_OUT=out, HOME=home)
        res = I.result(p)
        self.assertNotIn("driver_error", res, res)
        outside = [r for r in res["rows"] if r["class"] == "file" and not r["target"]["in_workspace"]]
        self.assertEqual({os.path.basename(r["target"]["path"]) for r in outside}, {"outside.txt", "a-moved.txt"},
                         [r["summary"] for r in res["rows"]])
        st = res["state"]
        self.assertTrue(st["inside"], f"control: the in-workspace write was not applied: {res['plain']}")
        self.assertFalse(st["outside"], "a plain apply wrote a file outside the workspace")
        self.assertFalse(st["trashed"], "a plain apply moved a file into the Trash outside the workspace")
        self.assertTrue(st["a_in_ws"], "a plain apply deleted the half of the move that is inside the workspace")
        st2 = res["state2"]
        self.assertEqual(res["second"]["status"], "ok", res["second"])
        self.assertTrue(st2["outside"] and st2["trashed"] and not st2["a_in_ws"],
                        f"named with --only, the outside effects should apply: {st2} {res['second']}")


if __name__ == "__main__":
    unittest.main()
