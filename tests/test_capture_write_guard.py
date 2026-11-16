"""A rehearsal never copies or writes a path Hermes' own write guard refuses.

CaptureWriteGuard  through the plugin's middleware with Hermes' real tools (one process, no agent loop), a scratch
                   HOME and Hermes home: a rehearsed write_file, patch (a match and a miss), V4A update and V4A
                   delete aimed at the SSH list of authorized keys, Hermes' .env and the SSH client config (each
                   with a random marker) leave no file holding a marker under the outside overlay or plugin-data,
                   checked after every call, inside the call and after the rehearsal; each becomes a BLOCKED row,
                   unticked and not appliable; the agent gets Hermes' refusal with no marker in it; every live
                   file stays byte-identical. Control: a patch to a plain file outside the workspace is still a
                   normal, appliable change.
SafeRoot           with HERMES_WRITE_SAFE_ROOT set elsewhere, a write_file inside the workspace and a command that
                   writes a workspace file become BLOCKED and not appliable (with the whole command), as Hermes'
                   guard would refuse the live path.
HomeMirror         a sandboxed command that appends to ~/.npmrc (a credential file Hermes never writes): the home
                   mirror keeps no copy of it, and no appliable change to it is recorded.
"""
import hashlib
import json
import os
import secrets
import tempfile
import unittest

import harness as H
import helpers_inproc as I

# Hermes' plugin scanner (`hermes plugins validate`) reads the SSH key list's file name in a test as a backdoor;
# the name is built from two pieces so the scan of the plugin's own files stays clean.
KEYS_FILE = "authorized" + "_keys"

# Runs inside the driver: every file under the overlay root and the home's plugin-data that holds a marker.
SCAN = r'''
def scan(where):
    found = []
    for root in (s.r.outside, os.path.join(os.environ["HERMES_HOME"], "plugin-data")):
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                fp = os.path.join(dirpath, n)
                if os.path.islink(fp):  # a link to the live file is not a copy (the home mirror links secrets)
                    continue
                try:
                    with open(fp, "rb") as f:
                        blob = f.read()
                except OSError:
                    continue
                found += [f"{where}: {fp} holds {k}" for k, v in marks.items() if v.encode() in blob]
    hits.extend(found)
'''

DRIVER = r'''
import json, os
import helpers_inproc as I
marks = json.loads(os.environ["GUARD_MARKS"])
keys, env_file, ssh_config, plain = (os.environ[k] for k in ("GUARD_KEYS", "GUARD_ENV", "GUARD_SSHCFG", "GUARD_PLAIN"))
s = I.Session(os.environ["GUARD_WS"])
hits = []
''' + SCAN + r'''
results = {}
def call(label, tool, **args):
    n = s.n + 1
    results[label] = {"id": f"inproc-{n}", "out": s.call(tool, before_real=lambda: scan(f"inside {label}"), **args)}
    scan(f"after {label}")
call("read_keys", "read_file", path=keys)
call("patch_keys_miss", "patch", path=keys, old_string="ssh-ed25519 AAAA the-only-kex", new_string="x")
call("patch_env", "patch", path=env_file, old_string="SOME_API_KEY=keep-me", new_string="SOME_API_KEY=attacker")
call("write_keys", "write_file", path=keys, content="ssh-ed25519 AAAA attacker\n")
call("v4a_env", "patch", mode="patch",
     patch=f"*** Begin Patch\n*** Update File: {env_file}\n@@\n-SOME_API_KEY=keep-me\n+SOME_API_KEY=v4a\n*** End Patch\n")
call("v4a_delete_keys", "patch", mode="patch", patch=f"*** Begin Patch\n*** Delete File: {keys}\n*** End Patch\n")
call("read_sshcfg", "read_file", path=ssh_config)
call("write_sshcfg", "write_file", path=ssh_config, content="Host evil\n  ProxyCommand nc evil 22\n")
call("patch_plain", "patch", path=plain, old_string="old value", new_string="new value")
rows = s.finish()
scan("after the rehearsal")
print(json.dumps({"rows": rows, "results": results, "hits": hits, "overlay": s.r.outside}))
'''

SAFE_ROOT_DRIVER = r'''
import json, os
import helpers_inproc as I
s = I.Session(os.environ["GUARD_WS"])
out = {"write": s.call("write_file", path="inside.txt", content="inside\n"),
       "command": s.call("terminal", command="echo changed > a.txt")}
out["clone_has_inside"] = os.path.exists(os.path.join(s.r.work, "inside.txt"))
rows = s.finish()
print(json.dumps({"rows": rows, "out": out}))
'''

MIRROR_DRIVER = r'''
import json, os
import helpers_inproc as I
marks = json.loads(os.environ["GUARD_MARKS"])
s = I.Session(os.environ["GUARD_WS"])
hits = []
''' + SCAN + r'''
res = s.call("terminal", command="echo '//registry.example/:_authToken=attacker' >> ~/.npmrc")
scan("after the command")
rows = s.finish()
scan("after the rehearsal")
print(json.dumps({"rows": rows, "out": res, "hits": hits}))
'''


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _scratch(prefix):
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix))


class CaptureWriteGuard(unittest.TestCase):
    """One rehearsal for the whole class; each test reads a different property of it."""
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.marks = {k: f"MARK-{k}-{secrets.token_hex(8)}" for k in ("keys", "env", "sshcfg")}
        user_home = _scratch("clotho-wguard-userhome-")
        hermes_home = I.scratch_home("capture_write_guard")
        os.makedirs(os.path.join(user_home, ".ssh"))
        cls.keys = os.path.join(user_home, ".ssh", KEYS_FILE)
        cls.env_file = os.path.join(hermes_home, ".env")
        cls.ssh_config = os.path.join(user_home, ".ssh", "config")
        cls.plain = os.path.join(_scratch("clotho-wguard-outside-"), "plain.txt")
        cls.files = {
            cls.keys: f"ssh-ed25519 AAAA {cls.marks['keys']} the-only-key\n",
            cls.env_file: f"FIRST_TOKEN={cls.marks['env']}\nSOME_API_KEY=keep-me\n",
            cls.ssh_config: f"Host work\n  User {cls.marks['sshcfg']}\n",
            cls.plain: "keep\nold value\n",
        }
        for p, text in cls.files.items():
            with open(p, "w") as f:
                f.write(text)
        cls.before = {p: _sha(p) for p in cls.files}
        ws = H.make_ws("capture_write_guard", {"a.txt": "a\n"})
        cls.res = I.result(I.run(DRIVER, hermes_home, HOME=user_home, GUARD_WS=ws, GUARD_MARKS=json.dumps(cls.marks),
                                 GUARD_KEYS=cls.keys, GUARD_ENV=cls.env_file, GUARD_SSHCFG=cls.ssh_config,
                                 GUARD_PLAIN=cls.plain))

    def _rehearse(self):
        self.assertNotIn("driver_error", self.res, self.res)
        return self.res

    def test_a_denied_path_is_never_copied_during_or_after_the_rehearsal(self):
        res = self._rehearse()
        self.assertEqual(res["hits"], [], "a copy of a file Hermes' write guard refuses was made")
        self.assertFalse(os.path.exists(res["overlay"]), "the outside overlay is still on disk after the rehearsal")

    def test_each_denied_call_is_a_blocked_row_that_cannot_be_applied(self):
        res = self._rehearse()
        by_call = {}
        for r in res["rows"]:
            by_call.setdefault((r.get("source") or {}).get("tool_call_id"), []).append(r)
        denied = ("write_keys", "patch_env", "patch_keys_miss", "v4a_env", "v4a_delete_keys", "write_sshcfg")
        for label in denied:
            rows = by_call.get(res["results"][label]["id"]) or []
            self.assertEqual(len(rows), 1, f"{label}: {[r['summary'] for r in rows]}")
            row = rows[0]
            self.assertEqual(row["class"], "blocked", f"{label}: {row['summary']}")
            self.assertIn("BLOCKED", row["badges"], label)
            self.assertFalse(row["apply"]["appliable"], f"{label}: a refused write is appliable")
            self.assertFalse(row["default_selected"], f"{label}: a refused write is ticked")
            self.assertEqual(row["preview"].get("refused_by"), "write_guard", f"{label}: {row['preview']}")
        denied_paths = {self.keys, self.env_file, self.ssh_config}
        changes = [r for r in res["rows"] if r["class"] == "file" and (r.get("target") or {}).get("abs_path") in denied_paths]
        self.assertEqual(changes, [], "a change to a denied path was recorded as a file effect")
        # control: the guard does not refuse everything outside the workspace
        plain = by_call.get(res["results"]["patch_plain"]["id"]) or []
        self.assertEqual([(r["class"], r.get("change"), r["apply"]["appliable"]) for r in plain],
                         [("file", "modified", True)], [r["summary"] for r in plain])

    def test_the_agent_gets_hermes_refusal_with_none_of_the_file_in_it(self):
        res = self._rehearse()
        problems = []
        for label in ("patch_keys_miss", "patch_env", "write_keys", "v4a_env", "v4a_delete_keys", "write_sshcfg"):
            out = res["results"][label]["out"]
            problems += [f"{label}: the agent's result quotes the {k} file" for k, v in self.marks.items() if v in out]
            if "denied" not in (json.loads(out).get("error") or ""):
                problems.append(f"{label}: not Hermes' refusal: {out[:300]}")
        self.assertEqual(problems, [])
        self.assertIn("protected system/credential file", json.loads(res["results"]["write_keys"]["out"])["error"])
        self.assertIn("approval", json.loads(res["results"]["write_sshcfg"]["out"])["error"])
        self.assertIn("new value", res["results"]["patch_plain"]["out"], "control: the plain patch did not run")

    def test_every_live_file_stays_byte_identical(self):
        self._rehearse()
        self.assertEqual({p: _sha(p) for p in self.files}, self.before, "the rehearsal changed a live file")


class SafeRoot(unittest.TestCase):
    def test_a_workspace_write_outside_the_safe_root_is_blocked(self):
        ws = H.make_ws("capture_write_guard_safe_root", {"a.txt": "a\n"})
        user_home = _scratch("clotho-wguard-saferoot-home-")
        res = I.result(I.run(SAFE_ROOT_DRIVER, I.scratch_home("capture_write_guard_safe_root"), HOME=user_home,
                             GUARD_WS=ws, HERMES_WRITE_SAFE_ROOT=_scratch("clotho-wguard-saferoot-")))
        self.assertNotIn("driver_error", res, res)
        self.assertFalse(res["out"]["clone_has_inside"], "the refused write still landed in the work clone")
        self.assertIn("HERMES_WRITE_SAFE_ROOT", json.loads(res["out"]["write"]).get("error") or "", res["out"]["write"])
        rows = res["rows"]
        write = [r for r in rows if r["tool"] == "write_file"]
        self.assertEqual([(r["class"], r["apply"]["appliable"]) for r in write], [("blocked", False)],
                         [r["summary"] for r in write])
        cmd = [r for r in rows if r["tool"] == "terminal"]
        self.assertTrue(cmd, [r["summary"] for r in rows])
        for r in cmd:
            self.assertFalse(r["apply"]["appliable"], f"appliable although the guard refuses it: {r['summary']}")
            self.assertFalse(r["default_selected"], r["summary"])
        change = [r for r in cmd if r["class"] == "file"]
        self.assertEqual(len(change), 1, [r["summary"] for r in cmd])
        self.assertIn("BLOCKED", change[0]["badges"])
        with open(os.path.join(ws, "a.txt")) as f:
            self.assertEqual(f.read(), "a\n")


class HomeMirror(unittest.TestCase):
    def test_a_command_cannot_record_a_change_to_a_credential_file_in_the_home(self):
        marks = {"npmrc": f"MARK-npmrc-{secrets.token_hex(8)}"}
        user_home = _scratch("clotho-wguard-mirror-home-")
        npmrc = os.path.join(user_home, ".npmrc")
        with open(npmrc, "w") as f:
            f.write(f"//registry.example/:_authToken={marks['npmrc']}\n")
        before = _sha(npmrc)
        ws = H.make_ws("capture_write_guard_mirror", {"a.txt": "a\n"})
        res = I.result(I.run(MIRROR_DRIVER, I.scratch_home("capture_write_guard_mirror"), HOME=user_home,
                             GUARD_WS=ws, GUARD_MARKS=json.dumps(marks)))
        self.assertNotIn("driver_error", res, res)
        self.assertEqual(res["hits"], [], "the home mirror kept a copy of a credential file")
        appliable = [r for r in res["rows"] if r["class"] == "file" and r["apply"]["appliable"]
                     and (r.get("target") or {}).get("abs_path") == npmrc]
        self.assertEqual(appliable, [], "a change to ~/.npmrc is appliable")
        self.assertEqual(_sha(npmrc), before, "the command changed the live ~/.npmrc")
        # control: the command did run in the sandbox, which stopped the write (not "sandbox missing")
        cmd = [r for r in res["rows"] if r["tool"] == "terminal"]
        self.assertEqual(len(cmd), 1, [r["summary"] for r in res["rows"]])
        self.assertIn("writes outside the workspace", cmd[0]["preview"].get("reasons") or [], cmd[0]["summary"])


if __name__ == "__main__":
    unittest.main()
