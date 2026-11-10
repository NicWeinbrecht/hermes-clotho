"""Cron mode without Hermes: the roll-up line, job lookup, the read-only job store and notepad readers,
and the refusals. No Hermes process, no network."""
import collections
import json
import os
import sqlite3
import tempfile
import unittest

import helpers_apply as HA

C = HA.sub("cron")
state = HA.sub("state")


def _bytes(p):
    with open(p, "rb") as f:
        return f.read()


def _text(p):
    with open(p) as f:
        return f.read()


def _file(i, path, sha="a"):
    return {"id": i, "class": "file", "change": "created", "target": {"path": path}, "after": {"sha256": sha},
            "default_selected": True, "apply": {"appliable": True}}


def _http(i, url, body="hello"):
    req = {"method": "POST", "url": url, "body": body}
    return {"id": i, "class": "http", "target": req, "preview": {"request": req}, "irreversible": True,
            "default_selected": True, "apply": {"appliable": True}}


def _mail(i, to):
    return {"id": i, "class": "outbound", "tool": "send_email", "target": {"recipients": [to]},
            "args": {"to": to, "body": "report"}, "irreversible": True, "default_selected": True,
            "apply": {"appliable": True}}


def _run(k, rows, status="finished", error=None):
    return {"index": k, "rid": f"r-20261110-10000{k}-abcd", "status": status, "rows": rows, "error": error}


BASE = [_file(1, "/w/digest.md"), _http(2, "http://127.0.0.1:1/hook")]


class Rollup(unittest.TestCase):

    def test_all_identical(self):
        roll = C.rollup([_run(1, BASE), _run(2, list(BASE)), _run(3, list(BASE))], planned=3)
        self.assertEqual(roll["line"], "3 runs: 3 identical")
        self.assertEqual([p["text"] for p in roll["per_run"]], ["2 effects (baseline)", "same as run 1",
                                                                "same as run 1"])

    def test_one_differs_by_an_email(self):
        roll = C.rollup([_run(1, BASE), _run(2, BASE + [_mail(3, "sam@example.com")]), _run(3, BASE)], planned=3)
        self.assertEqual(roll["line"], "3 runs: 2 identical, 1 differs: +1 message")
        self.assertEqual(roll["per_run"][1]["text"], "+1 message")

    def test_baseline_is_the_majority_not_the_first_run(self):
        roll = C.rollup([_run(1, BASE[:1]), _run(2, BASE), _run(3, BASE)], planned=3)
        self.assertEqual(roll["baseline"], 2)
        self.assertEqual(roll["line"], "3 runs: 2 identical, 1 differs: -1 web request")

    def test_same_target_other_content_is_a_change(self):
        other = [_file(1, "/w/digest.md"), _http(2, "http://127.0.0.1:1/hook", body="goodbye")]
        roll = C.rollup([_run(1, BASE), _run(2, other)], planned=2)
        self.assertEqual(roll["per_run"][1]["text"], "1 web request with other content")

    def test_refused_failed_and_not_started(self):
        roll = C.rollup([_run(1, [], status="refused", error="the pre-run script needs the network")], planned=3)
        self.assertEqual(roll["line"], "3 runs: 1 refused, 2 not started")
        roll = C.rollup([_run(1, BASE), _run(2, [], status="failed", error="exit 1")], planned=2)
        self.assertEqual(roll["line"], "2 runs: 1 identical, 1 failed")

    def test_command_groups_count_through_their_file_changes(self):
        group = {"id": 3, "class": "command", "tool": "terminal", "target": {"command": "rm a"}, "children": [4]}
        child = {"id": 4, "class": "file", "change": "deleted", "target": {"path": "/w/a"}, "parent_id": 3}
        roll = C.rollup([_run(1, BASE), _run(2, BASE + [group, child])], planned=2)
        self.assertEqual(roll["per_run"][1]["text"], "+1 delete")

    def test_rollup_text_names_state_and_where_reports_are(self):
        roll = C.rollup([_run(1, BASE)], planned=1)
        st = {"record": "x", "next_run_at": "2026-11-10T11:35", "last_run_at": None, "outputs": [], "runs": 0}
        roll.update(job_id="abc123", job_name="digest", batch="/data/cron-runs/c-1", deliver="telegram",
                    state_changes=C.state_changes(st, dict(st)), state_after=st)
        text = C.render_rollup(roll, width=200)
        self.assertIn("1 run: 1 identical", text)
        self.assertIn("delivery: held on every run (the job delivers to telegram)", text)
        self.assertIn("job state: untouched", text)
        self.assertIn("reports: c-1", text)
        self.assertIn("apply one:  hermes clotho apply r-20261110-100001-abcd", text)
        moved = dict(st, record="y", next_run_at="2026-11-10T13:35")
        self.assertEqual(C.state_changes(st, moved), ["its record changed (next_run_at)"])

    def test_rollup_shows_no_hermes_home_path(self):
        """The roll-up is printed (and recorded on screen): the batch folder is named by its id, under a
        placeholder for the Hermes home, never by its absolute path (which carries the user name)."""
        roll = C.rollup([_run(1, BASE)], planned=1)
        st = {"record": "x", "next_run_at": None, "last_run_at": None, "outputs": [], "runs": 0}
        with tempfile.TemporaryDirectory() as home:
            batch = os.path.join(home, "plugin-data", "clotho", "cron-runs", "c-20261110-135337-da3e")
            roll.update(job_id="abc123", job_name="digest", batch=batch, deliver="local",
                        state_changes=[], state_after=st)
            for width in (58, 200):
                text = C.render_rollup(roll, width=width, home=home)
                flat = "".join(text.split())   # wrapped pieces of a path joined again
                for spelling in (home, os.path.realpath(home)):
                    self.assertNotIn(spelling, flat, text)
                self.assertNotIn(os.path.expanduser("~"), flat, text)
                self.assertIn("reports: c-20261110-135337-da3e", text)
                self.assertIn("<Hermeshome>/plugin-data/clotho/cron-runs/", flat, text)
                self.assertTrue(all(len(l) <= width for l in text.splitlines()), text)
            self.assertIn("in <Hermes home>/plugin-data/clotho/cron-runs/", C.render_rollup(roll, width=58, home=home))
            # a home given as a symlinked or relative spelling of the same folder is still recognised
            rel = os.path.relpath(home)
            self.assertIn("<Hermes home>/", C.render_rollup(roll, width=200, home=rel))

    def test_rollup_outside_the_hermes_home_uses_tilde(self):
        roll = C.rollup([_run(1, BASE)], planned=1)
        user = os.path.expanduser("~")
        roll.update(job_id="abc123", job_name="digest", batch=os.path.join(user, "elsewhere", "c-9"),
                    deliver="local", state_changes=[], state_after={})
        text = C.render_rollup(roll, width=200, home="/nonexistent-hermes-home")
        self.assertIn("reports: c-9", text)
        self.assertIn("in ~/elsewhere/", text)
        self.assertNotIn(user + "/", text)


class Jobs(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="rt-cron-unit-")
        os.makedirs(os.path.join(self.home, "cron"))
        self.jobs = [{"id": "a1b2c3d4e5f6", "name": "Digest", "prompt": "p"},
                     {"id": "a1ffffffffff", "name": "other", "prompt": "q"},
                     {"id": "0123456789ab", "name": "dup", "prompt": "x"},
                     {"id": "0123456789cd", "name": "DUP", "prompt": "y"}]

    def _write(self, data, bom=False):
        p = os.path.join(self.home, "cron", "jobs.json")
        with open(p, "wb") as f:
            f.write((b"\xef\xbb\xbf" if bom else b"") + json.dumps(data).encode())
        return p

    def _listing(self):
        d = os.path.join(self.home, "cron")
        return sorted((n, os.stat(os.path.join(d, n)).st_mtime_ns) for n in os.listdir(d))

    def test_find_by_id_prefix_and_name(self):
        self.assertEqual(C.find_job("a1b2c3d4e5f6", self.jobs)["name"], "Digest")
        self.assertEqual(C.find_job("a1b", self.jobs)["name"], "Digest")
        self.assertEqual(C.find_job("digest", self.jobs)["id"], "a1b2c3d4e5f6")
        with self.assertRaises(state.Refused) as e:
            C.find_job("dup", self.jobs)
        self.assertIn("matches 2 jobs", str(e.exception))
        with self.assertRaises(state.Refused):
            C.find_job("a1", self.jobs)
        with self.assertRaises(state.Refused) as e:
            C.find_job("nope", self.jobs)
        self.assertIn("no cron job matches", str(e.exception))

    def test_store_is_read_without_writing(self):
        p = self._write({"jobs": {"k1": {"name": "keyed", "prompt": "p"}}, "updated_at": "x"}, bom=True)
        raw, before = _bytes(p), self._listing()
        jobs = C.load_jobs(self.home)
        self.assertEqual([(j["id"], j["name"]) for j in jobs], [("k1", "keyed")])
        self.assertEqual(_bytes(p), raw)
        self.assertEqual(self._listing(), before, "reading the store created or touched a file")

    def test_corrupt_store_is_refused_not_repaired(self):
        p = os.path.join(self.home, "cron", "jobs.json")
        with open(p, "w") as f:
            f.write("{not json")
        with self.assertRaises(state.Refused):
            C.load_jobs(self.home)
        self.assertEqual(_text(p), "{not json")

    def test_notepad_read_only(self):
        self.assertEqual(C._notes_readonly("a1", self.home), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "cron", "notepad.db")),
                         "reading a missing notepad created the database")
        db = os.path.join(self.home, "cron", "notepad.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cron_notepad (job_id TEXT, key TEXT, value TEXT, updated_at TEXT, "
                    "PRIMARY KEY (job_id, key))")
        con.execute("INSERT INTO cron_notepad VALUES ('a1', 'mood', 'sunny', 't')")
        con.commit()
        con.close()
        raw, before = _bytes(db), self._listing()
        notes = C._notes_readonly("a1", self.home)
        self.assertEqual([(n["key"], n["value"]) for n in notes], [("mood", "sunny")])
        self.assertEqual(_bytes(db), raw)
        self.assertEqual(self._listing(), before, "reading the notepad created a journal or touched the file")

    def test_refusals(self):
        self.assertIn("script-only job", " ".join(C.job_problems({"no_agent": True, "script": "x.sh"})))
        self.assertIn("monitor job", " ".join(C.job_problems({"prompt": "p", "monitor_url": "https://x"})))
        self.assertIn("base_url", " ".join(C.job_problems({"prompt": "p", "base_url": "https://x"})))
        self.assertIn("sandbox", " ".join(C.job_problems({"prompt": "p", "script": "a.sh"}, (False, "not macOS"))))
        self.assertIn("nothing to run", " ".join(C.job_problems({"prompt": " "})))
        self.assertEqual(C.job_problems({"prompt": "p", "script": "a.sh"}, (True, "")), [])

    def test_job_state_reads_record_outputs_and_history(self):
        self._write({"jobs": [{"id": "a1", "prompt": "p", "next_run_at": "n1", "last_run_at": None}]})
        os.makedirs(os.path.join(self.home, "cron", "output", "a1"))
        st = C.job_state("a1", self.home)
        self.assertEqual((st["next_run_at"], st["outputs"], st["runs"]), ("n1", [], None))
        with open(os.path.join(self.home, "cron", "output", "a1", "run.md"), "w") as f:
            f.write("x")
        self.assertEqual(C.state_changes(st, C.job_state("a1", self.home)), ["1 new output document(s)"])


class DiffWords(unittest.TestCase):

    def test_words(self):
        a = collections.Counter({("delete", "file:deleted::/a", ""): 2, ("message", "k", "p"): 1})
        b = collections.Counter({("message", "k", "q"): 1, ("web request", "h", "1"): 1})
        self.assertEqual(C.diff_words(a, b), "+1 web request, -2 deletes, 1 message with other content")
        self.assertEqual(C.diff_words(a, a), "same effects")


if __name__ == "__main__":
    unittest.main()
