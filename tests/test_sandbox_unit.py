"""Unit tests for the shell sandbox's detection (hard links out of the clone, late writers) and parsers.
No Hermes needed: sandbox.py and snapshot.py are standard-library only."""
import os
import sys
import time
import unittest

import harness as H
import names
import sandbox
import snapshot


@unittest.skipUnless(sandbox.available()[0], "needs macOS sandbox-exec")
class SandboxDetection(unittest.TestCase):
    def setUp(self):
        self.base = H.path("unit", f"case-{time.time_ns()}")
        os.makedirs(self.base)
        self.ws = os.path.join(self.base, "ws")
        os.makedirs(self.ws)
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("a\n")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "victim.txt"), "w") as f:
            f.write("secret\n")
        self.work = os.path.join(self.base, "work")
        snapshot.clonefile(self.ws, self.work)
        self.tmp = os.path.join(self.base, "tmp")

    def run_cmd(self, cmd):
        return sandbox.run(self.work, self.work, cmd, self.tmp, tag=f"{names.PLUGIN_NAME}-unit-{time.time_ns()}", timeout=20,
                           orig=self.ws)

    def test_hard_link_to_outside_file_is_flagged(self):
        r = self.run_cmd(f"ln {self.outside}/victim.txt ./hl; echo done")
        self.assertEqual(r["detection"], "unified-log")
        self.assertTrue(r["blocked_write"], r["violations"])
        self.assertIn("writes outside the workspace", sandbox.reasons_cant_preview(r, sandbox.static_scan("ln x y")))
        with open(os.path.join(self.outside, "victim.txt")) as f:
            self.assertEqual(f.read(), "secret\n")

    def test_late_writer_is_killed_and_flagged(self):
        cmd = "nohup sh -c 'sleep 1; echo late > late.txt' >/dev/null 2>&1 & echo launched"
        r = self.run_cmd(cmd)
        self.assertTrue(r["survivors"] or "background" in sandbox.static_scan(cmd)["flags"])
        self.assertIn("starts a background process", sandbox.reasons_cant_preview(r, sandbox.static_scan(cmd)))
        time.sleep(2)
        self.assertFalse(os.path.exists(os.path.join(self.work, "late.txt")), "the detached child wrote after return")

    def test_survivor_without_static_hint(self):
        # no '&', nohup, setsid or disown in the text: only the process-group check can see it
        # the child lets go of the output pipes, so the command returns while the child lives on
        cmd = ("python3 -c \"import os,time\nif os.fork()==0:\n    os.close(1); os.close(2)\n"
               "    time.sleep(1); open('late2.txt','w').write('x')\n\"")
        r = self.run_cmd(cmd)
        self.assertNotIn("background", sandbox.static_scan(cmd)["flags"])
        time.sleep(2)
        self.assertFalse(os.path.exists(os.path.join(self.work, "late2.txt")))
        self.assertTrue(r["survivors"], "a forked child outliving the command was not detected")

    def test_clean_command_effects(self):
        r = self.run_cmd("mv a.txt b.txt && echo hi > c.txt")
        self.assertEqual(sandbox.reasons_cant_preview(r, sandbox.static_scan("mv a.txt b.txt")), [])


class Parsers(unittest.TestCase):
    def test_parse_http(self):
        h = sandbox.parse_http("curl -s -X POST https://hooks.example/x -H 'Content-Type: text/plain' -d 'hello'")
        self.assertEqual((h["method"], h["url"], h["body"]), ("POST", "https://hooks.example/x", "hello"))
        self.assertEqual(sandbox.parse_http("curl https://a.example/b")["method"], "GET")
        self.assertEqual(sandbox.parse_http("curl -d x https://a.example")["method"], "POST")
        self.assertIsNone(sandbox.parse_http("curl -d x https://a.example && rm -rf /"))
        self.assertIsNone(sandbox.parse_http("curl $(cat url)"))
        self.assertIsNone(sandbox.parse_http("ls -la"))
        self.assertEqual(sandbox.parse_http("wget --post-data=a=1 http://h/x")["method"], "POST")

    def test_static_scan(self):
        self.assertIn("background", sandbox.static_scan("sleep 5 &")["flags"])
        self.assertNotIn("background", sandbox.static_scan("a && b")["flags"])
        self.assertIn("network", sandbox.static_scan("git push origin main")["flags"])
        self.assertIn("effect-binary", sandbox.static_scan("osascript -e 'x'")["flags"])

    def test_classify_violation_forms(self):
        v = [{"process": "ln", "pid": 1, "operation": "forbidden-link-priv<file-write*>", "target": "/x"}]
        self.assertTrue(sandbox.classify_violations(v, "t")["blocked_write"])


if __name__ == "__main__":
    unittest.main()
