"""Sandbox hardening: a blocked action never vanishes from the result, writes to the home land in an
overlay, curl's file arguments are surfaced, and processes that leave the session are still killed.
No Hermes run needed (sandbox.py and snapshot.py are standard library only)."""
import os
import shlex
import statistics
import time
import unittest

import helpers_sandbox as S
import sandbox

REPEAT = int(os.environ.get("SANDBOX_REPEAT", "20"))
PORT = 19019  # nothing listens here; the sandbox refuses the connection before it is tried
REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def py(code: str) -> str:
    return f"{shlex.quote(S.venv_python())} -c {shlex.quote(code)}"


@unittest.skipUnless(sandbox.available()[0], "needs macOS sandbox-exec")
class DetectionRepeat(unittest.TestCase):
    """The kernel's sandbox log drops or delays some reports for short-lived python processes."""

    def repeat(self, cmd_for, expect):
        c = S.Case("repeat")
        missed, secs = [], []
        for i in range(REPEAT):
            cmd = cmd_for(c, i)
            r = c.run(cmd)
            secs.append(r["seconds"])
            why = S.reasons(r, cmd)
            if not any(expect in w for w in why):
                missed.append((i, why, r["detection"], r["exit_code"], r["stderr"][-160:]))
        self.assertEqual(os.listdir(c.outside), [], "enforcement: nothing may land outside")
        print(f"\n    {REPEAT} runs, flagged {REPEAT - len(missed)}, mean {statistics.mean(secs):.3f} s/command")
        self.assertEqual(missed, [], f"{len(missed)}/{REPEAT} blocked actions missing from the result")

    def test_python_outside_write_always_flagged(self):
        self.repeat(lambda c, i: py(f"open({os.path.join(c.outside, 'o.txt')!r}, 'w')"),
                    "writes outside the workspace")

    def test_python_socket_always_flagged(self):
        self.repeat(lambda c, i: py(f"import socket; socket.create_connection(('127.0.0.1', {PORT}), timeout=2)"),
                    "needs the network")

    def test_python_swallowed_outside_write_always_flagged(self):
        # no traceback, exit 0: the output alone shows nothing
        self.repeat(lambda c, i: py(f"try:\n    open({os.path.join(c.outside, 'o.txt')!r}, 'w')\nexcept OSError:\n    pass"),
                    "writes outside the workspace")

    def test_python_swallowed_socket_always_flagged(self):
        self.repeat(lambda c, i: py("import socket\ntry:\n    socket.create_connection(('127.0.0.1', %d), timeout=2)\n"
                                    "except OSError:\n    pass" % PORT), "needs the network")

    def test_permission_error_in_output_fails_closed(self):
        c = S.Case("eperm")
        cmd = "echo 'cp: /Volumes/x/y: Operation not permitted' >&2; exit 1"
        r = c.run(cmd)
        self.assertTrue(any("may have been blocked" in w for w in S.reasons(r, cmd)), S.reasons(r, cmd))

    def test_unanswered_log_fails_closed(self):
        c = S.Case("nolog")
        saved = sandbox.LOG
        sandbox.close_streams()
        sandbox.LOG = "/usr/bin/false"
        try:
            r = c.run("echo hi")
        finally:
            sandbox.LOG = saved
            sandbox.close_streams()
        self.assertEqual(r["stdout"], "hi\n")
        self.assertTrue(any("may have been blocked" in w for w in S.reasons(r, "echo hi")), S.reasons(r, "echo hi"))

    def test_late_reports_are_kept_for_the_caller(self):
        c = S.Case("late")
        tag = c.tag()
        r = sandbox.run(c.work, c.work, "true", c.tmp, tag=tag, orig=c.ws)
        self.assertEqual(r["log_reader"], "stream")
        s = sandbox._streams[tag.rsplit("-", 1)[0] + "-"]
        with s.cond:  # a report that reaches the log after the command's result was returned
            s.msgs.setdefault(tag, []).append(f"Sandbox: python3.14(4242) deny(1) file-write-create /x/late.txt\n{tag}")
        late = sandbox.late_violations(tag)
        self.assertEqual([(v["tag"], v["target"]) for v in late], [(tag, "/x/late.txt")])

    def test_clean_command_stays_clean(self):
        c = S.Case("clean")
        cmd = "mv a.txt b.txt && echo hi > c.txt && " + py("print(open('b.txt').read())")
        r = c.run(cmd)
        self.assertEqual(S.reasons(r, cmd), [], r)


@unittest.skipUnless(sandbox.available()[0], "needs macOS sandbox-exec")
class HomeOverlay(unittest.TestCase):
    def test_move_to_trash_lands_in_overlay(self):
        c = S.Case("trash")
        trash = os.path.join(REAL_HOME, ".Trash")
        before = S.listing(trash)
        name = f"a-{c.rid}.txt"
        cmd = f"mv a.txt ~/.Trash/{name} && echo moved"
        r = c.run(cmd, home_overlay=c.home_overlay)
        self.assertEqual((r["exit_code"], r["stdout"]), (0, "moved\n"), r["stderr"])
        self.assertEqual(S.listing(trash), before, "the real ~/.Trash changed")
        self.assertFalse(os.path.lexists(os.path.join(trash, name)))
        self.assertFalse(os.path.exists(os.path.join(c.work, "a.txt")))
        self.assertEqual(S.reasons(r, cmd), [])
        hit = [o for o in r["outside_changes"] if o["abs_path"] == os.path.join(trash, name)]
        self.assertEqual(len(hit), 1, r["outside_changes"])
        self.assertEqual((hit[0]["change"], hit[0]["kind"]), ("created", "file"))
        with open(hit[0]["overlay_path"]) as f:
            self.assertEqual(f.read(), "a\n")
        # the next command of the same rehearsal sees the would-be state
        r2 = c.run(f"cat ~/.Trash/{name}", home_overlay=c.home_overlay)
        self.assertEqual(r2["stdout"], "a\n")
        self.assertEqual(r2["outside_changes"], [])

    def test_absolute_path_into_real_home_stays_blocked(self):
        c = S.Case("abshome")
        target = os.path.join(REAL_HOME, f".{c.rid}-must-not-exist")
        cmd = f"echo x > {target}"
        r = c.run(cmd, home_overlay=c.home_overlay)
        self.assertFalse(os.path.lexists(target))
        self.assertIn("writes outside the workspace", S.reasons(r, cmd))

    def test_linked_home_entries_cannot_be_moved_or_written_through(self):
        c = S.Case("links")
        d = next(n for n in sorted(os.listdir(REAL_HOME)) if n not in (".Trash",)
                 and os.path.isdir(os.path.join(REAL_HOME, n)) and not os.path.islink(os.path.join(REAL_HOME, n)))
        before = S.listing(REAL_HOME)
        cmd = f"mv ~/{shlex.quote(d)} ~/moved-{c.rid}"
        r = c.run(cmd, home_overlay=c.home_overlay)
        self.assertIn("writes outside the workspace", S.reasons(r, cmd))
        self.assertEqual(S.listing(REAL_HOME), before)
        self.assertEqual(r["outside_changes"], [])

    @unittest.skipUnless(os.path.isdir(os.path.join(REAL_HOME, ".ssh")), "no ~/.ssh")
    def test_secrets_stay_unreadable_through_the_overlay(self):
        c = S.Case("secret")
        r = c.run("ls ~/.ssh/", home_overlay=c.home_overlay)
        self.assertTrue(r["blocked_read"], r["violations"])
        self.assertEqual(r["stdout"], "")

    def test_overlay_rolls_back_when_the_command_cannot_be_previewed(self):
        c = S.Case("rollback")
        cmd = f"mv a.txt ~/.Trash/r-{c.rid}.txt; curl -s -m 2 http://127.0.0.1:{PORT}/x"
        r = c.run(cmd, home_overlay=c.home_overlay)
        self.assertIn("needs the network", S.reasons(r, cmd))
        self.assertEqual(r["outside_changes"], [])
        self.assertTrue(any(o["abs_path"].endswith(f"r-{c.rid}.txt") for o in r["outside_changes_discarded"]))
        r2 = c.run(f"ls ~/.Trash/r-{c.rid}.txt", home_overlay=c.home_overlay)
        self.assertNotEqual(r2["exit_code"], 0, "the discarded move is still visible to the next command")

    def test_output_names_the_real_home(self):
        c = S.Case("homeout")
        r = c.run("echo $HOME; ls -d ~/.Trash", home_overlay=c.home_overlay)
        self.assertEqual(r["stdout"].split(), [REAL_HOME, os.path.join(REAL_HOME, ".Trash")])

    def test_without_overlay_home_is_the_real_one(self):
        c = S.Case("nooverlay")
        r = c.run("echo $HOME")
        self.assertEqual(r["stdout"].strip(), os.environ.get("HOME"))
        self.assertEqual(r["outside_changes"], [])


class CurlFiles(unittest.TestCase):
    def files(self, cmd):
        s = sandbox.static_scan(cmd)
        return s.get("writes", []), s.get("reads", [])

    def test_output_flags_are_surfaced(self):
        self.assertEqual(self.files("curl -o page.html https://a.example/x")[0], ["page.html"])
        self.assertEqual(self.files("curl --output page.html https://a.example/x")[0], ["page.html"])
        self.assertEqual(self.files("curl -sSLo out.bin https://a.example/x")[0], ["out.bin"])
        self.assertEqual(self.files("curl -O https://a.example/dl/f.tgz")[0], ["f.tgz"])
        self.assertEqual(self.files("curl -D h.txt -c jar.txt https://a.example/")[0], ["h.txt", "jar.txt"])
        self.assertEqual(self.files("curl -K cfg.txt")[1], ["cfg.txt"])
        self.assertEqual(self.files("curl --config cfg.txt https://a.example/")[1], ["cfg.txt"])
        self.assertEqual(self.files("wget https://a.example/dl/f.tgz")[0], ["f.tgz"])
        self.assertEqual(self.files("wget -O x.html https://a.example/")[0], ["x.html"])

    def test_stdout_and_dev_null_are_not_files(self):
        self.assertEqual(self.files("curl -s -o /dev/null -w '%{http_code}' https://a.example/x"), ([], []))
        self.assertEqual(self.files("curl -o - https://a.example/x"), ([], []))
        self.assertEqual(self.files("wget -qO- https://a.example/x"), ([], []))
        self.assertEqual(self.files("curl -s -X POST -d hi https://a.example/x"), ([], []))

    def test_file_arguments_make_the_request_unpreviewable(self):
        scan = sandbox.static_scan("curl -o page.html https://a.example/x")
        why = sandbox.reasons_cant_preview({"blocked_network": True}, scan)
        self.assertNotEqual(why, ["needs the network"])  # so it is not a ticked web request that apply re-runs
        self.assertTrue(any("page.html" in w for w in why), why)
        scan = sandbox.static_scan("curl -K cfg.txt")
        self.assertTrue(any("cfg.txt" in w for w in sandbox.reasons_cant_preview({"blocked_network": True}, scan)))
        scan = sandbox.static_scan("curl -s -o /dev/null https://a.example/x")
        self.assertEqual(sandbox.reasons_cant_preview({"blocked_network": True}, scan), ["needs the network"])

    def test_parse_http_lists_the_files(self):
        h = sandbox.parse_http("curl -o page.html https://a.example/x")
        self.assertEqual((h["method"], h["url"], h["writes"]), ("GET", "https://a.example/x", ["page.html"]))

    @unittest.skipUnless(sandbox.available()[0], "needs macOS sandbox-exec")
    def test_sandboxed_curl_with_output_file(self):
        c = S.Case("curlo")
        cmd = f"curl -s -m 2 -o page.html http://127.0.0.1:{PORT}/x"
        r = c.run(cmd)
        why = S.reasons(r, cmd)
        self.assertIn("needs the network", why)
        self.assertTrue(any("page.html" in w for w in why), why)


@unittest.skipUnless(sandbox.available()[0], "needs macOS sandbox-exec")
class SessionLeavers(unittest.TestCase):
    def test_setsid_child_is_killed_and_reported(self):
        c = S.Case("setsid")
        code = ("import os, time\n"
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    os.close(0); os.close(1); os.close(2)\n"
                "    time.sleep(1.5)\n"
                "    open('late3.txt', 'w').write('x')\n")
        cmd = py(code)
        r = c.run(cmd)
        self.assertNotIn("background", sandbox.static_scan(cmd)["flags"])
        time.sleep(3)
        self.assertFalse(os.path.exists(os.path.join(c.work, "late3.txt")), "a setsid child acted after the return")
        self.assertTrue(r["survivors"], "the setsid child was not reported")
        self.assertIn("starts a background process", S.reasons(r, cmd))


if __name__ == "__main__":
    unittest.main()
