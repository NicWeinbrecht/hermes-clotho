"""Apply on synthetic rehearsals: dependencies (D27), partial apply, failures in the middle,
idempotence after a failure or a kill. No Hermes process, no network: a fake tool layer
(helpers_apply.FakeTools) stands in for Hermes' real tools and records what it "sent"."""
import argparse
import contextlib
import io
import json
import os
import unittest

import helpers_apply as HA


def _listing_run():
    """The reviewer's case: count.txt is created, `ls > listing.txt` lists it, a POST sends it
    with --data-binary @count.txt, then hello.txt is written."""
    run = HA.FakeRun()
    count = run.file("count.txt", "3\n")                                   # 1
    grp = run.command("ls > listing.txt")                                  # 2
    listing = run.file("listing.txt", "count.txt\n", parent=grp)          # 3
    post = run.http("curl -s -X POST http://127.0.0.1:9/hook --data-binary @count.txt",
                    "POST", "http://127.0.0.1:9/hook", body="3\n")         # 4
    run.depends(post, count)  # the ledger records this one: the command names count.txt
    run.file("hello.txt", "hello\n")                                       # 5
    run.finish()
    return run, count, listing, post


def _cli_apply(rid, **kw):
    cli = HA.sub("cli")
    ns = argparse.Namespace(id=rid, only=kw.get("only", ""), except_=kw.get("except_", ""), dry_run=False,
                            json=True, force=kw.get("force", False))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_apply(ns)
    return rc, json.loads(buf.getvalue())


class DependencyStop(unittest.TestCase):

    def test_ticked_effect_needing_unticked_one_stops_before_any_change(self):
        run, count, listing, post = _listing_run()
        tools = HA.FakeTools(run.ws)
        HA.setup(run, tools)
        apply = HA.sub("apply")
        before = sorted(os.listdir(run.ws))
        rc, receipt = _cli_apply(run.rid, except_=str(count["id"]))
        self.assertEqual(rc, apply.EXIT_DEPENDENCY, receipt)
        self.assertEqual(receipt["status"], "dependency")
        self.assertEqual(sorted(os.listdir(run.ws)), before, "a dependency stop must change nothing")
        self.assertEqual(tools.calls, [], "nothing may be sent")
        pairs = {(c["id"], c["needs"]): c for c in receipt["conflicts"]}
        self.assertIn((post["id"], count["id"]), pairs)
        self.assertTrue(pairs[(post["id"], count["id"])]["sure"])
        self.assertTrue(all(c["sure"] for c in receipt["conflicts"]), "only certain edges stop Apply (D34)")
        # the directory listing names count.txt: a guessed dependency, reported as one, not a stop
        guesses = {(c["id"], c["needs"]): c for c in receipt["guesses"]}
        self.assertIn((listing["id"], count["id"]), guesses, receipt)
        self.assertFalse(guesses[(listing["id"], count["id"])]["sure"])
        self.assertIn("count.txt", guesses[(listing["id"], count["id"])]["why"])

    def test_text_names_both_effects_and_the_way_out(self):
        run, count, listing, post = _listing_run()
        apply, report = HA.setup(run)
        rc = apply.apply(run.rid, except_=[count["id"]])
        text = HA.visible(report.render_receipt(rc, color=False))
        self.assertIn(f"#{post['id']}", text)
        self.assertIn(f"#{count['id']}", text)
        self.assertIn("--force", text)
        self.assertIn("guess", text)
        self.assertIn("before any change", text)

    def test_force_continues_and_receipt_lists_applied_failed_not_attempted(self):
        run, count, listing, post = _listing_run()
        tools = HA.FakeTools(run.ws)
        apply, _ = HA.setup(run, tools)
        rc = apply.apply(run.rid, except_=[count["id"]], force=True)
        self.assertEqual(rc["status"], "failed")
        by = {r["id"]: r for r in rc["results"]}
        self.assertEqual(by[listing["id"]]["result"], "applied")
        self.assertEqual(by[post["id"]]["result"], "failed")
        self.assertIn("exit code 26", by[post["id"]]["detail"])
        self.assertEqual(rc["not_attempted"], [5], rc)
        self.assertEqual(rc["applied_ids"], [listing["id"]])
        self.assertEqual(rc["failed_ids"], [post["id"]])
        self.assertTrue(rc["forced"])
        self.assertEqual(run.live("listing.txt"), "count.txt\n")
        self.assertIsNone(run.live("hello.txt"))

    def test_a_guessed_dependency_warns_and_carries_on(self):
        """D34: the agent read count.txt before posting a fixed text; the ledger ties the POST to it,
        a guess. Unticking count.txt must not stop the POST; the receipt says it is a guess."""
        run = HA.FakeRun()
        count = run.file("count.txt", "3\n")                                    # 1
        post = run.http("curl -s -X POST http://127.0.0.1:9/hook -d done", "POST",
                        "http://127.0.0.1:9/hook", body="done")                   # 2
        run.depends(post, count)  # "the agent read count.txt before this"
        run.finish()
        tools = HA.FakeTools(run.ws)
        apply, report = HA.setup(run, tools)
        rc = apply.apply(run.rid, except_=[count["id"]])
        self.assertEqual(rc["status"], "ok", rc)
        self.assertEqual(apply.exit_code(rc), apply.EXIT_OK)
        self.assertEqual(rc["conflicts"], [])
        self.assertFalse(rc["forced"])
        self.assertEqual([(g["id"], g["needs"], g["sure"]) for g in rc["guesses"]], [(post["id"], count["id"], False)])
        self.assertEqual(tools.sent, [("http", "http://127.0.0.1:9/hook")])
        self.assertIsNone(run.live("count.txt"))
        text = HA.visible(report.render_receipt(rc, color=False))
        self.assertRegex(text, r"(?s)guess.*#2.*#1")
        self.assertNotIn("stopped", text)
        dry = apply.apply(run.rid, except_=[count["id"]], dry_run=True)
        self.assertEqual(dry["status"], "dry-run")

    def test_dependency_already_applied_is_satisfied(self):
        run, count, listing, post = _listing_run()
        tools = HA.FakeTools(run.ws)
        apply, _ = HA.setup(run, tools)
        a = apply.apply(run.rid, only=[count["id"]])
        self.assertEqual(a["status"], "ok", a)
        b = apply.apply(run.rid, except_=[count["id"]])
        self.assertEqual(b["status"], "ok", b)
        self.assertEqual(b["conflicts"], [])
        self.assertEqual(tools.sent, [("http", "http://127.0.0.1:9/hook")])


class DependencyGuesses(unittest.TestCase):

    def test_folder_listing_without_content_match_is_a_guess(self):
        run = HA.FakeRun()
        c = run.file("data/count.txt", "3\n")
        g = run.command("tar czf backup.tgz data")
        b = run.file("backup.tgz", "\x1f\x8b binary", parent=g)
        run.finish()
        apply, _ = HA.setup(run)
        rows = HA.sub("ledger").load(os.path.join(run.dir, "ledger.jsonl"))
        edges = {(e["to"], e["from"]): e for e in apply.dependencies(rows, run.dir)}
        self.assertIn((b["id"], c["id"]), edges)
        self.assertFalse(edges[(b["id"], c["id"])]["sure"])

    def test_same_file_is_sure(self):
        run = HA.FakeRun()
        a = run.file("notes.txt", "one\n")
        b = run.file("notes.txt", "two\n", change="modified")
        run.depends(b, a)
        run.finish()
        apply, _ = HA.setup(run)
        rows = HA.sub("ledger").load(os.path.join(run.dir, "ledger.jsonl"))
        e = [x for x in apply.dependencies(rows, run.dir) if x["to"] == b["id"]]
        self.assertTrue(e and e[0]["sure"], e)

    def test_unrelated_effects_get_no_edge(self):
        run = HA.FakeRun()
        run.file("a.txt", "alpha\n")
        run.file("b.txt", "beta\n")
        run.outbound("send_message", "@sam", "hello there")
        run.finish()
        apply, _ = HA.setup(run)
        rows = HA.sub("ledger").load(os.path.join(run.dir, "ledger.jsonl"))
        self.assertEqual(apply.dependencies(rows, run.dir), [])


class FailureAndIdempotence(unittest.TestCase):

    def _three(self):
        run = HA.FakeRun()
        run.http("curl -s -X POST http://127.0.0.1:9/a -d one", "POST", "http://127.0.0.1:9/a", body="one")  # 1
        run.outbound("send_message", "@sam", "the report is ready")                                         # 2
        run.file("after.txt", "after\n")                                                                     # 3
        run.finish()
        return run

    def test_second_apply_after_a_failure_sends_nothing_twice(self):
        run = self._three()
        tools = HA.FakeTools(run.ws)
        tools.fail_tool["send_message"] = "platform unavailable"
        apply, report = HA.setup(run, tools)
        first = apply.apply(run.rid)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["applied_ids"], [1])
        self.assertEqual(first["failed_ids"], [2])
        self.assertEqual(first["not_attempted"], [3])
        self.assertIn("platform unavailable", {r["id"]: r for r in first["results"]}[2]["detail"])
        text = HA.visible(report.render_receipt(first, color=False))
        self.assertIn("not attempted", text)
        del tools.fail_tool["send_message"]
        second = apply.apply(run.rid)
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(tools.sent, [("http", "http://127.0.0.1:9/a"), ("send_message", "@sam")])
        third = apply.apply(run.rid)
        self.assertEqual(third["applied"], 0)
        self.assertEqual(len(tools.sent), 2, "a repeated apply must send nothing again")

    def test_kill_in_the_middle_leaves_a_journal_and_nothing_is_sent_twice(self):
        run = self._three()
        tools = HA.FakeTools(run.ws)
        tools.crash_on = 2  # dies while calling send_message
        apply, _ = HA.setup(run, tools)
        with self.assertRaises(KeyboardInterrupt):
            apply.apply(run.rid)
        tools.crash_on = None
        second = apply.apply(run.rid)
        by = {r["id"]: r for r in second["results"]}
        self.assertTrue(by[1]["result"].startswith("already applied"), by)
        self.assertEqual(by[2]["result"], "held", by)
        self.assertIn("--only 2", by[2]["detail"])
        self.assertEqual(by[3]["result"], "applied")
        self.assertEqual(second["status"], "incomplete")
        self.assertEqual(tools.sent, [("http", "http://127.0.0.1:9/a")], "the cut-off call must not be resent")
        third = apply.apply(run.rid, only=[2])
        self.assertEqual(third["status"], "ok", third)
        self.assertEqual(tools.sent, [("http", "http://127.0.0.1:9/a"), ("send_message", "@sam")])

    def test_request_that_reached_the_server_is_not_resent(self):
        run = self._three()
        tools = HA.FakeTools(run.ws)
        tools.exit_for["/a"] = 22
        apply, _ = HA.setup(run, tools)
        first = apply.apply(run.rid)
        self.assertEqual(first["failed_ids"], [1])
        tools.exit_for.clear()
        second = apply.apply(run.rid)
        self.assertEqual({r["id"]: r for r in second["results"]}[1]["result"], "held")
        self.assertEqual([s for s in tools.sent if s[0] == "http"], [("http", "http://127.0.0.1:9/a")])

    def test_request_that_never_left_is_retried(self):
        run = self._three()
        tools = HA.FakeTools(run.ws)
        tools.exit_for["/a"] = 7  # could not connect: nothing was sent
        apply, _ = HA.setup(run, tools)
        apply.apply(run.rid)
        tools.exit_for.clear()
        second = apply.apply(run.rid)
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual([s for s in tools.sent if s[0] == "http"], [("http", "http://127.0.0.1:9/a")])


class SymlinkedParent(unittest.TestCase):
    """A folder on an effect's path is swapped for a symlink after the rehearsal: the write,
    delete or move would land outside the workspace. Apply must refuse and leave the outside
    folder (a sentinel) exactly as it was."""

    def _outside(self, run, files=None):
        out = os.path.join(run.tmp, "outside")
        os.makedirs(out, exist_ok=True)
        for name, text in (files or {"sentinel.txt": "do not touch\n"}).items():
            with open(os.path.join(out, name), "w") as f:
                f.write(text)
        return out

    def _tree(self, folder):
        snap = {}
        for root, dirs, files in os.walk(folder):
            for n in files:
                p = os.path.join(root, n)
                snap[os.path.relpath(p, folder)] = HA.sha(open(p, "rb").read())
        return snap

    def _swap_for_link(self, run, rel, target):
        p = os.path.join(run.ws, rel)
        if os.path.isdir(p):
            os.rename(p, os.path.join(run.tmp, "moved-away-" + os.path.basename(rel)))
        os.symlink(target, p)

    def test_create_through_a_new_folder_link_is_refused(self):
        run = HA.FakeRun()
        os.makedirs(os.path.join(run.ws, "sub"))
        run.file("sub/new.txt", "made in the rehearsal\n")
        run.finish()
        out = self._outside(run)
        before = self._tree(out)
        self._swap_for_link(run, "sub", out)
        apply, report = HA.setup(run)
        rc = apply.apply(run.rid)
        self.assertEqual(self._tree(out), before, "apply wrote through a symlinked folder")
        self.assertNotEqual(rc["status"], "ok", rc)
        self.assertEqual(apply.exit_code(rc), apply.EXIT_DRIFT, rc)
        self.assertIn("symlink", json.dumps(rc.get("drift")), rc)
        self.assertIn("symlink", HA.visible(report.render_receipt(rc, color=False)))

    def test_delete_through_a_new_folder_link_is_refused(self):
        run = HA.FakeRun()
        run.put("sub/old.txt", "same bytes\n")
        run.file("sub/old.txt", change="deleted")
        run.finish()
        # the outside folder holds a file with the same name and content: the drift check alone is fooled
        out = self._outside(run, {"old.txt": "same bytes\n", "sentinel.txt": "do not touch\n"})
        before = self._tree(out)
        self._swap_for_link(run, "sub", out)
        apply, _ = HA.setup(run)
        rc = apply.apply(run.rid)
        self.assertEqual(self._tree(out), before, "apply deleted through a symlinked folder")
        self.assertEqual(apply.exit_code(rc), apply.EXIT_DRIFT, rc)

    def test_move_through_a_new_folder_link_is_refused(self):
        run = HA.FakeRun()
        run.put("old.txt", "moving\n")
        os.makedirs(os.path.join(run.ws, "archive"))
        g = run.command("mv old.txt archive/old.txt")
        run.file("archive/old.txt", "moving\n", change="renamed", parent=g, from_path="old.txt")
        run.finish()
        out = self._outside(run)
        before = self._tree(out)
        self._swap_for_link(run, "archive", out)
        apply, _ = HA.setup(run)
        rc = apply.apply(run.rid)
        self.assertEqual(self._tree(out), before, "apply moved a file through a symlinked folder")
        self.assertEqual(run.live("old.txt"), "moving\n")
        self.assertEqual(apply.exit_code(rc), apply.EXIT_DRIFT, rc)

    def test_link_made_after_the_checks_is_still_refused_at_write_time(self):
        """The link appears between the drift check and the write: each write checks again."""
        run = HA.FakeRun()
        os.makedirs(os.path.join(run.ws, "sub"))
        r1 = run.file("sub/new.txt", "made in the rehearsal\n")
        run.put("gone/old.txt", "same bytes\n")
        r2 = run.file("gone/old.txt", change="deleted")
        run.finish()
        out = self._outside(run, {"old.txt": "same bytes\n", "new.txt": "outside original\n"})
        before = self._tree(out)
        apply, _ = HA.setup(run)
        self._swap_for_link(run, "sub", out)
        self._swap_for_link(run, "gone", out)
        for r in (r1, r2):
            with self.assertRaises(apply.UnsafePath):
                apply._apply_file(run.dir, r)
        self.assertEqual(self._tree(out), before)

    def test_plain_folders_still_apply(self):
        run = HA.FakeRun()
        os.makedirs(os.path.join(run.ws, "sub"))
        run.file("sub/deeper/new.txt", "fine\n")
        run.finish()
        apply, _ = HA.setup(run)
        rc = apply.apply(run.rid)
        self.assertEqual(rc["status"], "ok", rc)
        self.assertEqual(run.live("sub/deeper/new.txt"), "fine\n")


class OneAtATime(unittest.TestCase):

    def test_second_apply_while_one_runs_is_refused(self):
        import fcntl
        run = HA.FakeRun()
        run.outbound("send_message", "@sam", "hello")
        run.finish()
        tools = HA.FakeTools(run.ws)
        apply, _ = HA.setup(run, tools)
        fd = os.open(os.path.join(run.dir, "apply.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # stands in for an apply in another terminal
            with self.assertRaises(RuntimeError):
                apply.apply(run.rid)
            self.assertEqual(tools.sent, [])
            cli = HA.sub("cli")
            ns = argparse.Namespace(id=run.rid, only="", except_="", dry_run=False, json=True, force=False)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.cmd_apply(ns)
            self.assertEqual(rc, apply.EXIT_REFUSED)
        finally:
            os.close(fd)
        self.assertEqual(apply.apply(run.rid)["status"], "ok")
        self.assertEqual(tools.sent, [("send_message", "@sam")])


class Folders(unittest.TestCase):

    def test_created_folders_are_made_empty_or_not(self):
        """`mkdir Data && mkdir -p Docs/old && mv a.txt Docs/`: folder rows have target.kind "dir" and no blob."""
        run = HA.FakeRun()
        run.put("a.txt", "a\n")
        grp = run.command("mkdir Data && mkdir -p Docs/old && mv a.txt Docs/")
        dirs = []
        for rel in ("Data", "Docs", "Docs/old"):
            row = run.file(rel, change="created", parent=grp)
            row["target"]["kind"] = "dir"
            row["after"], row["blob"], row["preview"] = {"exists": True, "kind": "dir", "mode": "0o750"}, None, {}
            dirs.append(row)
        run.file("Docs/a.txt", "a\n", change="renamed", parent=grp, from_path="a.txt")
        run.finish()
        apply, _ = HA.setup(run)
        rc, receipt = _cli_apply(run.rid)
        self.assertEqual(rc, apply.EXIT_OK, receipt)
        self.assertEqual(receipt["failed_ids"], [], receipt)
        for rel in ("Data", "Docs", "Docs/old"):
            p = os.path.join(run.ws, rel)
            self.assertTrue(os.path.isdir(p) and not os.path.islink(p), rel)
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o750", rel)
        self.assertEqual(os.listdir(os.path.join(run.ws, "Data")), [])
        self.assertEqual(run.live("Docs/a.txt"), "a\n")
        self.assertFalse(os.path.exists(os.path.join(run.ws, "a.txt")))
        rc2, again = _cli_apply(run.rid)  # a repeat is a no-op, not a failure
        self.assertEqual(rc2, apply.EXIT_OK, again)
        self.assertEqual(again["applied"], 0, again)

    def test_a_file_where_the_folder_should_go_is_drift(self):
        run = HA.FakeRun()
        grp = run.command("mkdir Data")
        row = run.file("Data", change="created", parent=grp)
        row["target"]["kind"] = "dir"
        row["after"], row["blob"], row["preview"] = {"exists": True, "kind": "dir", "mode": "0o755"}, None, {}
        run.finish()
        run.put("Data", "now a file\n")
        apply, _ = HA.setup(run)
        rc, receipt = _cli_apply(run.rid)
        self.assertEqual(rc, apply.EXIT_DRIFT, receipt)
        self.assertEqual(run.live("Data"), "now a file\n")


if __name__ == "__main__":
    unittest.main()
