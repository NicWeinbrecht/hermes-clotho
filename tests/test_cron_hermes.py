"""Cron mode through Hermes' real job store, prompt builder, agent loop and middleware (scripted provider,
no model): `hermes clotho cron <job> --runs N` on a job made with `hermes cron create`.

Instruments: the local sink (outbound requests), snapshot.tree_hash over the whole <home>/cron folder
(jobs.json with next/last run, executions.db history, output documents, notepad.db, the usage audit that
records each delivery target) and over the workspace, and the scripted provider's request log (which
tools each run was offered). Control: the same job run by `hermes cron run` with the plugin disabled
must reach the sink, write the workspace file and change the cron folder.
"""
import json
import os
import re
import stat
import time
import unittest

import harness as H
import names
from harness import call

RID = re.compile(r"r-\d{8}-\d{6}-[0-9a-f]{4}")


def _bytes(p):
    with open(p, "rb") as f:
        return f.read()


def _text(p):
    with open(p) as f:
        return f.read()


def setUpModule():
    H.require_home()
    H.start_servers()
    assert H.sink_selftest(), "the sink does not log requests: the instrument is broken"
    _seed_bundled_skills()


def _seed_bundled_skills():
    """Hermes copies its bundled skills into a home when a gateway starts (gateway/run_bootstrap.py), and a
    real scheduled run always runs in a started gateway. A fresh test home in which no gateway has started
    yet has none, so the job's skill would be missing only here. Seed them with Hermes' own command."""
    want = os.path.join(_skills_dir(), "productivity", SKILL)
    if os.path.isdir(want):
        return
    p = H.hermes(["skills", "opt-in", "--sync"], cwd=H.root(), label="cron_seed_skills")
    assert p.returncode == 0 and os.path.isdir(want), \
        f"could not seed Hermes' bundled skills: {p.stdout[-800:]}{p.stderr[-800:]}"


def tearDownModule():
    H.stop_servers()


def _cron_dir():
    return os.path.join(H.HOME, "cron")


SKILL = "maps"  # a skill bundled with Hermes (<home>/skills/productivity/maps)


def _skills_dir():
    return os.path.join(H.HOME, "skills")


def _jobs():
    p = os.path.join(_cron_dir(), "jobs.json")
    with open(p) as f:
        return json.load(f)["jobs"]


def _job_by_name(name):
    hits = [j for j in _jobs() if j.get("name") == name]
    assert len(hits) == 1, f"expected one job named {name}, got {len(hits)}"
    return hits[0]


def _create(name, prompt, ws, *extra, label):
    p = H.hermes(["cron", "create", "--name", name, "--deliver", "local", "--workdir", ws, "--model", "scripted",
                  "--provider", H.STUB_PROVIDER, *extra, "every 2h", prompt], cwd=ws, label=label)
    assert p.returncode == 0, p.stdout[-1500:] + p.stderr[-1500:]
    return _job_by_name(name)


def _stub_rows():
    return H.lines(H.STUB_LOG)


def _offered_tools(rows):
    """The tool list of each conversation's first request (the stub logs it only there)."""
    return [tuple(r["tools"]) for r in rows if r.get("tools")]


def _rids(text):
    return list(dict.fromkeys(RID.findall(text)))


def _gateway_stop(label):
    H.hermes(["gateway", "stop"], cwd=H.root(), label=label)


def _script(name, body):
    d = os.path.join(H.HOME, "scripts")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return p


class TCron(unittest.TestCase):
    """A cron job's run as a rehearsal: 0 requests, job state byte-identical, delivery held, ledger
    holds the effects; the control job, run on Hermes' own cron path, does all of it for real."""

    def test_cron_runs_capture_and_leave_the_job_alone(self):
        stamp = time.strftime("%H%M%S")
        steps = [[call("write_file", path="digest.md", content="# digest\nthree new tickets\n")],
                 [call("terminal", command=f"curl -s -X POST http://127.0.0.1:{H.SINK_PORT}/cron/main "
                                           f"-d 'cron digest {stamp}'")]]
        mark = H.script("cron_main", steps, "Digest written and posted.")
        ws = H.make_ws("cron_main", {"notes.txt": "monday\n"})
        name = f"cron-main-{stamp}"
        job = _create(name, f"{mark} write the digest and post it", ws, "--skill", SKILL, label="cron_create")
        jid = job["id"]
        n = H.hermes(["cron", "notepad", jid, "set", "mood", "sunny-" + stamp], cwd=ws, label="cron_notepad")
        self.assertEqual(n.returncode, 0, n.stderr[-1000:])
        _gateway_stop("cron_gw_stop0")
        cron_before, ws_before, skills_before = H.tree(_cron_dir()), H.tree(ws), H.tree(_skills_dir())
        jobs_before = _bytes(os.path.join(_cron_dir(), "jobs.json"))
        stub0 = len(_stub_rows())
        ids0 = H.rehearsal_ids()

        p = H.hermes([H.CLI, "cron", jid, "--runs", "2", "--no-diffs"], cwd=ws, label="cron_rt")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])

        # nothing happened
        self.assertEqual(H.sink_requests("/cron/main"), [], "a request escaped during the cron rehearsal")
        self.assertEqual(H.tree(ws), ws_before, "the workspace changed during the cron rehearsal")
        self.assertEqual(_bytes(os.path.join(_cron_dir(), "jobs.json")), jobs_before,
                         "jobs.json (next run, last run, counters) changed")
        self.assertEqual(H.tree(_cron_dir()), cron_before,
                         "the cron folder changed (job record, history, outputs, notepad or delivery audit)")
        self.assertEqual(H.tree(_skills_dir()), skills_before, "loading the job's skill bumped Hermes' usage counters")

        # one rehearsal per run, each with its own ledger holding the effects
        rids = _rids(p.stdout)
        self.assertEqual(len(rids), 2, p.stdout[-2000:])
        self.assertEqual(set(rids), H.rehearsal_ids() - ids0)
        for k, rid in enumerate(rids, 1):
            rows = H.ledger(rid)
            files = [r for r in rows if r["class"] == "file"]
            http = [r for r in rows if r["class"] == "http"]
            self.assertTrue(any(r["target"]["path"].endswith("digest.md") for r in files), rows)
            self.assertEqual(len(http), 1, rows)
            self.assertIn(f"/cron/main", json.dumps(http[0]))
            m = H.meta(rid)
            self.assertEqual(m["status"], "finished")
            self.assertEqual((m["cron"]["job_id"], m["cron"]["run"], m["cron"]["of"]), (jid, k, 2))
            self.assertEqual(m["cron"]["delivery"], {"target": "local", "held": True, "would_deliver": True,
                                                     "silent": False})
            prompt = _text(os.path.join(H.rdir(rid), "cron_prompt.txt"))
            self.assertIn("You are running as a scheduled cron job", prompt)   # Hermes' own cron prompt
            self.assertIn("sunny-" + stamp, prompt)                             # notepad, read only
            self.assertIn(f'invoked the "{SKILL}" skill', prompt)                # the job's skill
        batch = os.path.join(H.HOME, "plugin-data", names.PLUGIN_NAME, "cron-runs", H.meta(rids[0])["cron"]["batch"])
        for f in ("run-1.report.txt", "run-2.report.txt", "rollup.txt", "batch.json"):
            self.assertTrue(os.path.exists(os.path.join(batch, f)), f)
        roll = _text(os.path.join(batch, "rollup.txt"))
        self.assertIn("2 runs: 2 identical", roll)
        self.assertIn("job state: untouched", roll)
        self.assertIn("2 runs: 2 identical", p.stdout)
        # the printed roll-up (recorded on screen) names the batch by its id, never the Hermes home's path
        printed = p.stdout[p.stdout.rindex(f"{names.PRODUCT} cron · "):]   # the roll-up, printed last
        flat = "".join(printed.split())
        for spelling in (H.HOME, os.path.realpath(H.HOME), os.path.expanduser("~")):
            self.assertNotIn("".join(spelling.split()), flat, printed)
        self.assertIn("reports: " + os.path.basename(batch), printed)
        rt_tools = _offered_tools(_stub_rows()[stub0:])
        self.assertEqual(len(rt_tools), 2)

        # control: Hermes' own cron path with the plugin off does it all for real
        stub1 = len(_stub_rows())
        try:
            d = H.hermes(["plugins", "disable", names.PLUGIN_NAME], cwd=ws, label="cron_plugin_off")
            self.assertEqual(d.returncode, 0, d.stderr[-1000:])
            _gateway_stop("cron_gw_stop1")
            c = H.hermes(["cron", "run", jid], cwd=ws, label="cron_control")
            self.assertEqual(c.returncode, 0, c.stdout[-1500:] + c.stderr[-1500:])
            for _ in range(120):
                if H.sink_requests("/cron/main"):
                    break
                time.sleep(0.5)
        finally:
            H.hermes(["plugins", "enable", names.PLUGIN_NAME], cwd=ws, label="cron_plugin_on")
            _gateway_stop("cron_gw_stop2")
        self.assertEqual(len(H.sink_requests("/cron/main")), 1, "the control run should reach the sink")
        self.assertTrue(os.path.exists(os.path.join(ws, "digest.md")), "the control run should write the file")
        self.assertNotEqual(H.tree(_cron_dir()), cron_before, "the control run should change the cron folder")
        self.assertNotEqual(H.tree(_skills_dir()), skills_before, "the control run should count the skill's use")
        after = _job_by_name(name)
        self.assertNotEqual(after.get("last_run_at"), job.get("last_run_at"), "the control should record a run")
        outs = os.listdir(os.path.join(_cron_dir(), "output", jid))
        self.assertEqual(len(outs), 1, "the control run should save its output document")
        audit = [json.loads(x) for x in _text(os.path.join(_cron_dir(), "usage_audit.jsonl")).splitlines() if x.strip()]
        self.assertEqual([a.get("deliver_target") for a in audit if a.get("job_id") == jid], ["local"])
        ctl_tools = _offered_tools(_stub_rows()[stub1:])
        self.assertEqual(len(ctl_tools), 1)
        self.assertEqual(rt_tools[0], ctl_tools[0], "the rehearsal offered other tools than a real cron run")

    def test_prerun_script_runs_in_the_sandbox(self):
        stamp = time.strftime("%H%M%S")
        mark = H.script("cron_script_ok", [[call("read_file", path="notes.txt")]], "Summarised.")
        ws = H.make_ws("cron_script_ok", {"notes.txt": "tuesday\n"})
        sname = f"rt_collect_{stamp}.sh"
        _script(sname, f"#!/bin/bash\necho 'inbox: 3 new from ops {stamp}'\n")
        job = _create(f"rt-cron-script-{stamp}", f"{mark} summarise the inbox", ws, "--script", sname,
                      label="cron_create_script")
        _gateway_stop("cron_gw_stop3")
        before = H.tree(_cron_dir())
        p = H.hermes([H.CLI, "cron", job["id"], "--no-diffs"], cwd=ws, label="cron_rt_script")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        rid = _rids(p.stdout)[0]
        prompt = _text(os.path.join(H.rdir(rid), "cron_prompt.txt"))
        self.assertIn(f"inbox: 3 new from ops {stamp}", prompt, "the script's output was not injected")
        self.assertIn("Script Output", prompt)
        self.assertEqual(H.meta(rid)["cron_script"]["exit_code"], 0)
        self.assertEqual(H.meta(rid)["cron_script"]["reasons"], [])
        self.assertEqual(H.tree(_cron_dir()), before)

    def test_prerun_script_that_needs_the_network_is_refused(self):
        stamp = time.strftime("%H%M%S")
        mark = H.script("cron_script_net", [[call("read_file", path="notes.txt")]], "Done.")
        ws = H.make_ws("cron_script_net", {"notes.txt": "wednesday\n"})
        sname = f"rt_post_{stamp}.sh"
        body = (f"#!/bin/bash\ncurl -s -X POST http://127.0.0.1:{H.SINK_PORT}/cron/script -d 'from the script "
                f"{stamp}'\necho posted\n")
        spath = _script(sname, body)
        job = _create(f"rt-cron-net-{stamp}", f"{mark} report what the script fetched", ws, "--script", sname,
                      label="cron_create_net")
        _gateway_stop("cron_gw_stop4")
        before, stub0 = H.tree(_cron_dir()), len(_stub_rows())
        p = H.hermes([H.CLI, "cron", job["id"], "--runs", "3", "--width", "200"], cwd=ws, label="cron_rt_net")
        self.assertEqual(p.returncode, 3, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("cannot run without doing something for real", p.stdout)
        self.assertIn("needs the network", p.stdout)
        self.assertIn("3 runs: 1 refused, 2 not started", p.stdout)
        self.assertIn("delivery: none (no run reached the agent)", p.stdout)
        self.assertEqual(H.sink_requests("/cron/script"), [], "the script reached the network")
        self.assertEqual(len(_stub_rows()), stub0, "the model was called although the script was refused")
        self.assertEqual(H.tree(_cron_dir()), before)
        rid = _rids(p.stdout)[0]
        self.assertEqual(H.meta(rid)["status"], "refused")
        # control: the same script outside the sandbox does reach the sink
        import subprocess
        subprocess.run(["/bin/bash", spath], capture_output=True, timeout=30)
        self.assertEqual(len(H.sink_requests("/cron/script")), 1, "the control script should reach the sink")

    def test_script_only_job_is_refused(self):
        stamp = time.strftime("%H%M%S")
        ws = H.make_ws("cron_noagent", {"notes.txt": "x\n"})
        sname = f"rt_watch_{stamp}.sh"
        _script(sname, "#!/bin/bash\necho alert\n")
        job = _create(f"rt-cron-noagent-{stamp}", "", ws, "--script", sname, "--no-agent",
                      label="cron_create_noagent")
        before, ids0 = H.tree(_cron_dir()), H.rehearsal_ids()
        p = H.hermes([H.CLI, "cron", job["id"]], cwd=ws, label="cron_rt_noagent")
        self.assertEqual(p.returncode, 3, p.stdout[-1500:] + p.stderr[-1500:])
        self.assertIn("script-only job", p.stderr)
        self.assertEqual(H.rehearsal_ids(), ids0)
        self.assertEqual(H.tree(_cron_dir()), before)


def _notepad_files():
    """{name: (bytes, mtime_ns)} of notepad.db and its -wal/-shm/-journal files that exist now."""
    out = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = os.path.join(_cron_dir(), "notepad.db" + suffix)
        if os.path.exists(p):
            out["notepad.db" + suffix] = (_bytes(p), os.stat(p).st_mtime_ns)
    return out


def _notepad_aside(label):
    """Move the notepad database (and its journal files) out of the cron folder, into this run's trash."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = os.path.join(_cron_dir(), "notepad.db" + suffix)
        if os.path.exists(p):
            os.replace(p, H.path("trash", f"{label}-notepad.db{suffix}"))


class TCronNotepad(unittest.TestCase):
    """`hermes clotho cron` never creates or writes Hermes' notepad database: a missing one stays missing,
    an existing one keeps its bytes and its mtime, and no -wal/-shm/-journal file is left next to it.
    Control: Hermes' own notepad reader (`hermes cron notepad <job> list`) creates the missing database."""

    def test_notepad_database_is_never_created_or_written(self):
        stamp = time.strftime("%H%M%S")
        mark = H.script("cron_np", [[call("read_file", path="notes.txt")]], "Noted.")
        ws = H.make_ws("cron_np", {"notes.txt": "thursday\n"})
        jid = _create(f"rt-cron-np-{stamp}", f"{mark} read the notes", ws, label="cron_create_np")["id"]
        _gateway_stop("cron_gw_stop_np")

        # 1. no notepad database: it stays missing
        _notepad_aside("np-missing")
        self.assertEqual(_notepad_files(), {})
        p = H.hermes([H.CLI, "cron", jid, "--no-diffs"], cwd=ws, label="cron_rt_np_missing")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertEqual(_notepad_files(), {}, "the rehearsal created Hermes' notepad database")
        prompt = _text(os.path.join(H.rdir(_rids(p.stdout)[0]), "cron_prompt.txt"))
        self.assertIn("You are running as a scheduled cron job", prompt)
        self.assertNotIn("Job notepad", prompt)

        # 2. an existing notepad database: same bytes, same mtime, no journal files; its note is in the prompt
        n = H.hermes(["cron", "notepad", jid, "set", "cursor", "c-" + stamp], cwd=ws, label="cron_np_set")
        self.assertEqual(n.returncode, 0, n.stderr[-1000:])
        before = _notepad_files()
        self.assertEqual(sorted(before), ["notepad.db"], before.keys())
        time.sleep(1.1)  # an mtime written during the run would differ even on a coarse clock
        p = H.hermes([H.CLI, "cron", jid, "--no-diffs"], cwd=ws, label="cron_rt_np_existing")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertEqual(_notepad_files(), before, "the rehearsal wrote Hermes' notepad database or left a journal")
        prompt = _text(os.path.join(H.rdir(_rids(p.stdout)[0]), "cron_prompt.txt"))
        self.assertIn("## Job notepad (persistent across runs)", prompt)
        self.assertIn("- cursor: c-" + stamp, prompt)

        # control: Hermes' own reader creates the database when it is missing (the instrument can see it)
        _notepad_aside("np-control")
        c = H.hermes(["cron", "notepad", jid, "list"], cwd=ws, label="cron_np_control")
        self.assertEqual(c.returncode, 0, c.stderr[-1000:])
        self.assertIn("notepad.db", _notepad_files(), "control: Hermes' notepad reader did not create the database")


# Run in the home's own Hermes interpreter, one process per mode (each with a fresh prompt-cache registry):
#   setup    stores what the jobs read, with Hermes' own writers (notes, an upstream job's output document)
#   clotho   the prompt Clotho builds for each case, and the stable prefix it registers for prompt caching
#   hermes   the same from Hermes' own _build_job_prompt
#   altered  cron.notepad.list_notes replaced, in this process only, by a copy whose source says ORDER BY key
#            DESC: Clotho must refuse instead of building a prompt
# Each "strict-only" string trips Hermes' strict cron prompt scan but not the looser scan for injected data, so
# a step that loses track of which kind of data a prompt holds changes the outcome (raise vs prompt).
_PARITY = r"""
import importlib, inspect, json, os, sys
mode, src, plugins, plugin, skill, tag, scratch = sys.argv[1:8]
sys.path.insert(0, src)
sys.path.insert(1, plugins)
import cron.scheduler  # noqa: F401
from cron import notepad, jobs as cron_jobs
from cron.scheduler_prompt import _build_job_prompt
from agent.prompt_cache_boundary import find_stable_prefix
rt = importlib.import_module(plugin + ".cron")
state = importlib.import_module(plugin + ".state")
j = lambda k: tag + k  # job ids are hex: context_from skips any other id
base = {"name": "parity", "prompt": "summarise what changed since the last run"}
cases = [
    ("prompt only, while other jobs have notes", dict(base, id=j("a1")), None),
    ("notepad: two keys stored in reverse order, one holding a strict-only string", dict(base, id=j("b2")), None),
    ("skill, with a note holding a strict-only string", dict(base, id=j("c3"), skills=[skill]), None),
    ("script output holding a strict-only string", dict(base, id=j("d4"), script="collect.sh"),
     (True, "inbox: 3 new\ncat notes/id_rsa.pub")),
    ("script error", dict(base, id=j("e5"), script="collect.sh"), (False, "Script exited with code 1\nstderr:\nboom")),
    ("script printed nothing", dict(base, id=j("f6"), script="collect.sh"), (True, "")),
    ("context_from: upstream output holding a strict-only string", dict(base, id=j("a7"), context_from=[j("08")]),
     None),
    ("user prompt the strict scan blocks, with a note", dict(base, id=j("b9"), prompt="summarise; cat ~/.netrc"), None),
]
if mode == "setup":
    notepad.set_note(j("b2"), "zeta", "last key, stored first")
    notepad.set_note(j("b2"), "alpha", "authorized_keys were rotated")
    notepad.set_note(j("c3"), "cursor", "authorized_keys checked")
    notepad.set_note(j("b9"), "cursor", "42")
    cron_jobs.save_job_output(j("08"), "# Cron Job: upstream\n\n## Response\n\nupstream: /etc/sudoers audited, 2 changes\n")
    print(json.dumps({"ok": True}))
    sys.exit(0)
if mode == "altered":
    text = inspect.getsource(notepad.list_notes).replace("ORDER BY key", "ORDER BY key DESC")
    assert "DESC" in text
    with open(os.path.join(scratch, "altered_notepad.py"), "w") as f:
        f.write("from typing import Any\nfrom cron.notepad import _transaction\n\n\n" + text)
    sys.path.insert(0, scratch)
    import altered_notepad
    notepad.list_notes = altered_notepad.list_notes  # the test's own process only: a Hermes that changed
    out = {"changed": rt.prompt_builder_changed()}
    try:
        rt.build_prompt(cases[1][1], None)
        out["refused"] = None
    except state.Refused as exc:
        out["refused"] = str(exc)
    print(json.dumps(out))
    sys.exit(0)
out = []
for label, job, prerun in cases:
    try:
        text = rt.build_prompt(job, prerun) if mode == "clotho" else _build_job_prompt(job, prerun_script=prerun)
        res = {"prompt": text, "prefix": find_stable_prefix(text) if text else None}
    except Exception as exc:
        res = {"raised": type(exc).__name__, "message": str(exc)}
    out.append(dict(res, case=label))
print(json.dumps(out))
"""


class TCronPromptParity(unittest.TestCase):
    """The prompt Clotho builds for a cron job is the one Hermes' own _build_job_prompt builds for it on this
    Hermes version, for every step Clotho re-implements (script output and errors, upstream output, the
    notepad with its key order and its job filter, skills and the prompt-cache prefix they register, the hint
    and run time, and the scan tier each kind of data gets); and a Hermes whose notepad reader changed is
    refused instead of drifting silently."""

    def _mode(self, mode, tag):
        import subprocess
        src = os.path.dirname(H.HERMES_CMD[-1])
        argv = [H.HERMES_CMD[0], "-B", "-c", _PARITY, mode, src, os.path.join(H.HOME, "plugins"), names.PLUGIN_NAME,
                SKILL, tag, H.path("scripts")]
        p = subprocess.run(argv, cwd=H.root(), env=H.env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, f"{mode}: {p.stdout[-3000:]}{p.stderr[-3000:]}")
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_prompt_matches_hermes_and_a_changed_builder_is_refused(self):
        import secrets
        _seed_bundled_skills()
        tag = secrets.token_hex(4)
        self._mode("setup", tag)
        first, hermes, second = self._mode("clotho", tag), self._mode("hermes", tag), self._mode("clotho", tag)
        for a, h, c in zip(first, hermes, second):
            # the run-time line has minute resolution: one of the two Clotho runs is in Hermes' minute
            self.assertIn(h, (a, c), f"{h['case']}: Clotho differs from Hermes\n--- hermes\n{json.dumps(h, indent=1)}"
                                     f"\n--- clotho\n{json.dumps(a, indent=1)}")
        by = {h["case"]: h for h in hermes}
        # the cases exercise what they say (on Hermes' own result)
        notes = by["notepad: two keys stored in reverse order, one holding a strict-only string"]["prompt"]
        self.assertLess(notes.index("- alpha: "), notes.index("- zeta: "))
        self.assertNotIn("Job notepad", by["prompt only, while other jobs have notes"]["prompt"])
        skilled = by["skill, with a note holding a strict-only string"]
        self.assertIn('invoked the "maps" skill', skilled["prompt"])
        self.assertIn("authorized_keys checked", skilled["prompt"])
        self.assertTrue(skilled["prefix"], "the skill case registers no stable prefix")
        self.assertIn("cat notes/id_rsa.pub", by["script output holding a strict-only string"]["prompt"])
        self.assertIn("Script Error", by["script error"]["prompt"])
        self.assertIsNone(by["script printed nothing"]["prompt"])
        upstream = by["context_from: upstream output holding a strict-only string"]["prompt"]
        self.assertIn("upstream: /etc/sudoers audited, 2 changes", upstream)
        self.assertIn("Output from job", upstream)
        self.assertEqual(by["user prompt the strict scan blocks, with a note"].get("raised"), "CronPromptInjectionBlocked")

        altered = self._mode("altered", tag)
        self.assertEqual(altered["changed"], ["cron.notepad.list_notes"], altered)
        self.assertIn("cron.notepad.list_notes changed", altered["refused"] or "", altered)


class TCronPromptBuilderChanged(unittest.TestCase):
    """A Hermes whose prompt builder is not the one this version follows (test seam: Clotho expects another
    cron.notepad.list_notes): `hermes clotho cron` refuses with exit 3 and says why, before anything runs: no
    rehearsal, no model call, Hermes' cron folder and the workspace untouched."""

    def test_changed_builder_is_refused_before_anything_runs(self):
        stamp = time.strftime("%H%M%S")
        mark = H.script("cron_changed", [[call("read_file", path="notes.txt")]], "Done.")
        ws = H.make_ws("cron_changed", {"notes.txt": "friday\n"})
        jid = _create(f"rt-cron-changed-{stamp}", f"{mark} read the notes", ws, label="cron_create_changed")["id"]
        n = H.hermes(["cron", "notepad", jid, "set", "cursor", "c-" + stamp], cwd=ws, label="cron_changed_np")
        self.assertEqual(n.returncode, 0, n.stderr[-1000:])
        _gateway_stop("cron_gw_stop_changed")
        cron_before, ws_before, np_before = H.tree(_cron_dir()), H.tree(ws), _notepad_files()
        stub0, ids0 = len(_stub_rows()), H.rehearsal_ids()
        p = H.hermes([H.CLI, "cron", jid, "--runs", "2"], cwd=ws, label="cron_rt_changed",
                     **{names.env("TEST_FAULT"): "prompt-builder"})
        self.assertEqual(p.returncode, 3, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("Hermes' cron prompt builder is not the one this", p.stdout + p.stderr)
        self.assertIn("cron.notepad.list_notes changed", p.stdout + p.stderr)
        self.assertEqual(len(_stub_rows()), stub0, "the model was called although the builder changed")
        self.assertEqual(H.rehearsal_ids(), ids0, "a rehearsal was started although the builder changed")
        self.assertEqual(H.tree(_cron_dir()), cron_before)
        self.assertEqual(_notepad_files(), np_before)
        self.assertEqual(H.tree(ws), ws_before)


if __name__ == "__main__":
    unittest.main()
