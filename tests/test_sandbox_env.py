"""A rehearsed command starts from an allowlisted environment, not from the environment Hermes runs with.

SandboxEnvUnit  sandbox.run directly: `env` does not print a variable set in the parent, while PATH, HOME and
                extra_env still reach the command
SandboxEnvRun   through the plugin's middleware and Hermes' real terminal tool: a rehearsed `env` does not print a
                variable set in the Hermes process, neither to the agent nor into the rehearsal's records (control:
                the variable is in that process's environment)
"""
import json
import os
import secrets
import unittest

import harness as H
import helpers_inproc as I
import helpers_sandbox as S
import names
import sandbox

CANARY_NAME = "CLOTHO_CANARY_SECRET"


@unittest.skipUnless(sandbox.available()[0], "needs macOS sandbox-exec")
class SandboxEnvUnit(unittest.TestCase):
    def setUp(self):
        self.value = "canary-" + secrets.token_hex(8)
        self.saved = os.environ.get(CANARY_NAME)
        os.environ[CANARY_NAME] = self.value

    def tearDown(self):
        if self.saved is None:
            os.environ.pop(CANARY_NAME, None)
        else:
            os.environ[CANARY_NAME] = self.saved

    def test_env_does_not_print_a_parent_variable(self):
        c = S.Case("envcanary")
        r = c.run("env")
        self.assertEqual(r["exit_code"], 0, r["stderr"])
        # assertFalse, not assertNotIn: a failure must not print the environment it found
        self.assertFalse(self.value in r["stdout"] + r["stderr"], "a rehearsed `env` printed the parent's variable")
        names_seen = {ln.split("=", 1)[0] for ln in r["stdout"].splitlines() if "=" in ln}
        self.assertIn("PATH", names_seen, "control: the command got no PATH")
        self.assertIn("HOME", names_seen, "control: the command got no HOME")

    def test_extra_env_still_reaches_the_command(self):
        c = S.Case("envextra")
        token = "extra-" + secrets.token_hex(6)
        r = c.run("printf %s \"$CLOTHO_EXTRA_PROBE\"", extra_env={"CLOTHO_EXTRA_PROBE": token})
        self.assertEqual(r["stdout"], token)


DRIVER = r'''
import json, os, subprocess
import helpers_inproc as I
s = I.Session(os.environ["ENV_WS"])
res = s.call("terminal", command="env")
rows = s.finish()
live = subprocess.run(["env"], capture_output=True, text=True).stdout  # control: this process does have it
print(json.dumps({"result": res, "rows": rows, "rdir": s.r.dir, "control": os.environ["CLOTHO_CANARY_SECRET"] in live}))
'''


class SandboxEnvRun(unittest.TestCase):
    """Through the plugin's middleware and Hermes' real terminal tool (one process, no agent loop)."""

    def test_rehearsed_env_does_not_print_a_variable_of_the_hermes_process(self):
        value = "canary-" + secrets.token_hex(8)
        ws = H.make_ws("env_canary", {"a.txt": "a\n"})
        p = I.run(DRIVER, I.scratch_home("env_canary"), ENV_WS=ws, **{CANARY_NAME: value})
        out = I.result(p)
        self.assertNotIn("driver_error", out, out)
        self.assertTrue(out["control"], "control: the variable is not in the driver's own environment")
        self.assertEqual(out["rows"], [], "a command that changes no file leaves no row")
        printed = json.loads(out["result"])["output"].splitlines()
        self.assertIn("PATH", {ln.split("=", 1)[0] for ln in printed}, "control: the rehearsed `env` printed nothing")
        self.assertIn(f"{names.SANDBOX_ENV}=1", printed, "control: the command did not run in the rehearsal's sandbox")
        self.assertFalse(value in out["result"], "the agent got the Hermes process's variable back")
        hits = []
        for dirpath, _dirs, files in os.walk(out["rdir"]):
            for f in files:
                fp = os.path.join(dirpath, f)
                with open(fp, "rb") as fh:
                    if value.encode() in fh.read():
                        hits.append(os.path.relpath(fp, out["rdir"]))
        self.assertEqual(hits, [], "the Hermes process's variable reached the rehearsal's records")


if __name__ == "__main__":
    unittest.main()
