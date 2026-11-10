"""`apply` and `report` with no id, on synthetic rehearsals (no Hermes process, no network).

ForkOrCronNewest   the newest rehearsal is a fork timeline or a cron-batch run: a bare `apply` (also
                   `--dry-run`, `last`) stops with exit 3, names every id to choose from and the exact command,
                   changes nothing (workspace bytes, no receipt, nothing sent) and never reaches apply
PlainNewest        the newest rehearsal is a plain one: a bare `apply` applies it as before, a fork or cron
                   run older than it does not matter
NoRehearsal        no rehearsal at all: `apply` and `report` say so in plain words (no 'None', no 'last')
ReportDefault      `report` with no id still shows the newest rehearsal, a fork timeline included
"""
import argparse
import contextlib
import io
import json
import os
import re
import tempfile
import unittest

import helpers_apply as HA

names = HA.sub("names")

FID = "f-20261110-090000-ab12"
BATCH = "c-20261110-100000-cd34"
URL = "http://127.0.0.1:9/hook"


def _ws_bytes(ws):
    out = {}
    for dp, _dn, fn in os.walk(ws):
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, ws)] = fh.read()
    return out


def _bytes(p):
    with open(p, "rb") as f:
        return f.read()


def _run(tmp, rid, text, **meta):
    """A finished rehearsal in the shared data folder: one file write and one POST."""
    run = HA.FakeRun(tmp=tmp, rid=rid)
    run.file(f"{rid[-4:]}.txt", text)
    run.http(f"curl -s -X POST {URL} -d '{text}'", "POST", URL, body=text)
    run.finish(**meta)
    return run


def _fork_json(data, fid, timelines):
    d = os.path.join(data, "forks", fid)
    os.makedirs(d)
    rec = {"schema": "clotho.fork.v1", "id": fid, "task": "synthetic", "status": "finished", "mode": "at once",
           "timelines": [{"name": n, "rid": r, "status": "finished", "model_calls": 2} for n, r in timelines]}
    with open(os.path.join(d, "fork.json"), "w") as f:
        json.dump(rec, f)
    return os.path.join(d, "fork.json")


def _ns(rid=None, dry_run=False, json_out=False):
    return argparse.Namespace(id=rid, only="", except_="", dry_run=dry_run, force=False, json=json_out,
                              no_diffs=True, width=None, expand=False)


def _call(fn, ns):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(ns)
    return rc, out.getvalue(), err.getvalue()


def _handle(ns, cmd):
    ns.clotho_cmd = cmd
    return HA.sub("cli").handle(ns)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clotho-default-test-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.tools = HA.FakeTools(os.path.realpath(os.path.join(self.tmp, "ws")))
        state = HA.sub("state")
        self._old_root = state.data_root
        state.data_root = lambda: self.data
        self.apply_mod = HA.sub("apply")
        self._old_tool = self.apply_mod._real_tool
        self.apply_mod._real_tool = self.tools
        self.reached_apply = []
        self._old_apply = self.apply_mod.apply

        def spy(rid, **kw):
            self.reached_apply.append(rid)
            return self._old_apply(rid, **kw)
        self.apply_mod.apply = spy

    def tearDown(self):
        HA.sub("state").data_root = self._old_root
        self.apply_mod._real_tool = self._old_tool
        self.apply_mod.apply = self._old_apply

    def receipts(self, rid):
        d = os.path.join(self.data, rid, "receipts")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


class ForkOrCronNewest(_Base):
    def _fork(self):
        self.plain = _run(self.tmp, "r-20261110-080000-0001", "plain\n").rid
        self.a = _run(self.tmp, "r-20261110-090001-00aa", "a\n", fork_id=FID, timeline="tidy").rid
        self.b = _run(self.tmp, "r-20261110-090001-00ff", "b\n", fork_id=FID, timeline="space").rid
        self.fork_file = _fork_json(self.data, FID, [("tidy", self.a), ("space", self.b)])
        self.ws = os.path.realpath(os.path.join(self.tmp, "ws"))

    def _assert_stopped(self, rc, out, err, choices):
        self.assertEqual(rc, self.apply_mod.EXIT_REFUSED, out + err)
        self.assertEqual(out, "")
        for spec in choices:
            self.assertIn(f"hermes {names.CLI_NAME} apply {spec}", " ".join(err.split()).replace("\\ ", ""))
        self.assertIn("nothing was changed", err)
        self.assertNotIn("None", err)
        self.assertEqual([ln for ln in err.splitlines() if len(ln) > 58], [], "lines over 58 columns")

    def test_bare_apply_after_a_fork_stops_and_changes_nothing(self):
        self._fork()
        ws0, fork0 = _ws_bytes(self.ws), _bytes(self.fork_file)
        for ns in (_ns(), _ns(dry_run=True), _ns("last"), _ns(json_out=True)):
            rc, out, err = _call(HA.sub("cli").cmd_apply, ns)
            self._assert_stopped(rc, out, err, [f"{FID}/tidy", f"{FID}/space"])
            self.assertIn(f"timeline space of fork {FID}", " ".join(err.split()))
        rc, out, err = _call(lambda ns: _handle(ns, "apply"), _ns())  # the `hermes clotho apply` entry
        self._assert_stopped(rc, out, err, [f"{FID}/tidy", f"{FID}/space"])
        self.assertEqual(self.reached_apply, [], "a bare apply reached apply()")
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(self.tools.sent, [], "something was sent")
        self.assertEqual(_ws_bytes(self.ws), ws0, "the workspace changed")
        self.assertEqual(_bytes(self.fork_file), fork0, "fork.json changed")
        for rid in (self.plain, self.a, self.b):
            self.assertEqual(self.receipts(rid), [], rid)

    def test_a_named_timeline_or_id_still_applies(self):
        self._fork()
        rc, out, err = _call(HA.sub("cli").handle, argparse.Namespace(clotho_cmd="apply", **vars(_ns(f"{FID}/tidy"))))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.reached_apply, [self.a])
        self.assertEqual(self.tools.sent, [("http", URL)])

    def test_bare_apply_after_a_cron_batch_stops_and_changes_nothing(self):
        self.plain = _run(self.tmp, "r-20261110-080000-0001", "plain\n").rid
        runs = [_run(self.tmp, f"r-20261110-10000{k}-00{k}0", f"run {k}\n",
                     cron={"job_id": "j1", "job_name": "daily-digest", "run": k, "of": 2, "batch": BATCH}).rid
                for k in (1, 2)]
        ws = os.path.realpath(os.path.join(self.tmp, "ws"))
        ws0 = _ws_bytes(ws)
        for ns in (_ns(), _ns(dry_run=True)):
            rc, out, err = _call(HA.sub("cli").cmd_apply, ns)
            self._assert_stopped(rc, out, err, runs)
            self.assertIn(f"run 2 of 2 of cron batch {BATCH}", " ".join(err.split()))
        self.assertEqual(self.reached_apply, [])
        self.assertEqual(self.tools.sent, [])
        self.assertEqual(_ws_bytes(ws), ws0)
        for rid in runs + [self.plain]:
            self.assertEqual(self.receipts(rid), [], rid)


class PlainNewest(_Base):
    def test_bare_apply_applies_the_newest_plain_rehearsal(self):
        a = _run(self.tmp, "r-20261110-090001-00aa", "a\n", fork_id=FID, timeline="tidy").rid
        _fork_json(self.data, FID, [("tidy", a)])
        _run(self.tmp, "r-20261110-095959-0bbb", "c\n", cron={"run": 1, "of": 1, "batch": BATCH})
        plain = _run(self.tmp, "r-20261110-110000-0001", "plain\n").rid
        rc, out, err = _call(HA.sub("cli").cmd_apply, _ns(json_out=True))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["status"], "ok")
        self.assertEqual(self.reached_apply, [plain])
        self.assertEqual(self.tools.sent, [("http", URL)])
        self.assertEqual(len(self.receipts(plain)), 1)

    def test_bare_apply_prints_what_the_id_form_prints(self):
        """Same output, byte for byte, as `apply <id>` (two identical runs, each applied once)."""
        texts = []
        for how in (None, "r-20261110-110000-0002"):
            self.tmp = tempfile.mkdtemp(prefix="clotho-default-test-")  # data_root follows self.data
            self.data = os.path.join(self.tmp, "data")
            os.makedirs(self.data)
            self.reached_apply.clear()
            self.tools.sent.clear()
            rid = _run(self.tmp, "r-20261110-110000-0002", "plain\n").rid
            rc, out, err = _call(HA.sub("cli").cmd_apply, _ns(how))
            self.assertEqual((rc, err), (0, ""), out + err)
            texts.append(re.sub(r"\d+\.\d+ s\b", "<t> s", out.replace(self.tmp, "<tmp>")))  # wall time aside
            self.assertEqual(self.reached_apply, [rid])
            self.assertEqual(self.tools.sent, [("http", URL)])
        self.assertIn("applied 2/2", texts[0])
        self.assertEqual(texts[0], texts[1])


class NoRehearsal(_Base):
    def test_apply_and_report_say_there_is_none(self):
        cli = HA.sub("cli")
        for fn, ns in ((cli.cmd_apply, _ns()), (cli.cmd_apply, _ns(dry_run=True)), (cli.cmd_report, _ns("last"))):
            rc, out, err = _call(fn, ns)
            self.assertNotEqual(rc, 0)
            self.assertEqual(out, "")
            self.assertNotIn("None", err)
            self.assertNotIn("'last'", err)
            self.assertIn("no rehearsals yet", err)
            self.assertIn(f"hermes {names.CLI_NAME} run", err)
            self.assertEqual([ln for ln in err.splitlines() if len(ln) > 58], [])
        self.assertEqual(self.reached_apply, [])

    def test_a_named_id_that_matches_nothing_keeps_its_message(self):
        _run(self.tmp, "r-20261110-080000-0001", "plain\n")
        rc, out, err = _call(HA.sub("cli").cmd_apply, _ns("r-nope"))
        self.assertEqual((rc, err.strip()), (self.apply_mod.EXIT_BAD_ID, "no rehearsal matches 'r-nope'"))


class ReportDefault(_Base):
    def test_report_with_no_id_shows_the_newest_even_a_timeline(self):
        _run(self.tmp, "r-20261110-080000-0001", "plain\n")
        b = _run(self.tmp, "r-20261110-090001-00ff", "b\n", fork_id=FID, timeline="space").rid
        _fork_json(self.data, FID, [("space", b)])
        rc, out, err = _call(HA.sub("cli").cmd_report, _ns("last"))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(b, out.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
