"""Forks without Hermes: timeline specs, the drop rule, the comparison and its rendering, the model-call cap.

ForkSpecs       --models / --variants parsing, aliases, unique timeline names, the prompt each one gets
ForkDrop        a provider answer saying the model cannot use tools (seen for nousresearch/hermes-4-405b) drops
                the timeline with that text as the reason; a clean exit is "finished"
ForkCompare     synthetic ledgers of 3 timelines: differences phrased in plain words, same-target content
                differences, lines within the width, plain text without a TTY or with NO_COLOR, the
                colour contract (no red before a timeline is kept; then the kept block red with "● keep"
                and the others dim, the badge unchanged inside both), 3 x 400 effects rendered in under 1 s
ForkCapUnit     (home Python, it has openai) calls over the cap get a synthetic stop and never reach the
                provider; when no stop reply can be built the process ends instead of calling the provider
"""
import importlib
import io
import json
import os
import tempfile
import time
import unittest

import ansi_contract as AC
import harness as H
import helpers_capture as C
import helpers_fork as F
import names

pkg = C.load_plugin_package()
fork = importlib.import_module(pkg.__name__ + ".fork")
compare = importlib.import_module(pkg.__name__ + ".compare")
report = importlib.import_module(pkg.__name__ + ".report")

PLAN = "claude-subscription-directsdk-experimental"
HERMES_405B = ("Model 'nousresearch/hermes-4-405b' isn't available on OpenRouter. Provider said: HTTP 404: "
               "No endpoints found that support tool use. Try disabling \"browser_exec\".")


class _TmpHome:
    def __enter__(self):
        self.old = os.environ.get("HERMES_HOME")
        self.d = tempfile.mkdtemp(prefix="fork-unit-", dir=H.path("scripts") if H.HOME else None)
        os.environ["HERMES_HOME"] = self.d
        return self.d

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old
        H.move_aside(self.d)


class ForkSpecs(unittest.TestCase):
    def test_models(self):
        t = fork.parse_model("deepseek/deepseek-v4-pro")
        self.assertEqual((t["name"], t["model"], t["provider"]), ("deepseek-v4-pro", "deepseek/deepseek-v4-pro", None))
        t = fork.parse_model("claude")
        self.assertEqual((t["name"], t["model"], t["provider"]), ("claude-sonnet", "sonnet", PLAN))
        t = fork.parse_model("fast=flash")
        self.assertEqual((t["name"], t["model"]), ("fast", "deepseek/deepseek-v4-flash"))
        t = fork.parse_model("x/y z@openrouter")
        self.assertEqual((t["name"], t["model"], t["provider"]), ("y-z", "x/y z", "openrouter"))
        tls = fork._dedupe([fork.parse_model("flash"), fork.parse_model("flash"), fork.parse_model("flash")])
        self.assertEqual([t["name"] for t in tls], ["deepseek-v4-flash", "deepseek-v4-flash-2", "deepseek-v4-flash-3"])

    def test_aliases_name_their_provider(self):
        """Seen in testing: `kimi` with no provider went to 'nvidia' in a home with no default
        provider and failed "No usable credentials found for provider 'nvidia'"."""
        for alias, model in (("flash", "deepseek/deepseek-v4-flash"), ("pro", "deepseek/deepseek-v4-pro"),
                             ("kimi", "moonshotai/kimi-k2.6")):
            t = fork.parse_model(alias)
            self.assertEqual((t["model"], t["provider"]), (model, "openrouter"), alias)

    def test_pin_provider_rules(self):
        def pin(model, picked, creds, default=None, provider=None):
            t = {"name": "t", "model": model, "provider": provider}
            return fork.pin_provider(t, choice=lambda m: (picked, m), has_credentials=lambda p: p in creds,
                                     home_default=lambda: default)
        t = pin("moonshotai/kimi-k2.6", "nvidia", {"openrouter"})          # no default provider in the home
        self.assertEqual(t["provider"], "openrouter")
        self.assertIn("nvidia", t["provider_from"])
        t = pin("moonshotai/kimi-k2.6", "nvidia", {"nvidia", "openrouter"})  # Hermes' own pick, usable
        self.assertEqual(t["provider"], "nvidia")
        t = pin("deepseek/deepseek-v4-flash", None, {"openrouter"}, default="nous")  # stays on the default
        self.assertEqual(t["provider"], "nous")
        t = pin("some-local-model", None, set())                          # nothing to decide here
        self.assertIsNone(t["provider"])
        t = pin("x/y", "nvidia", set(), provider="mine")                  # a given provider is never changed
        self.assertEqual((t["provider"], t["provider_from"]), ("mine", "given"))

    def test_variants(self):
        p = F.variants_file("unit_variants", [{"name": "tidy", "instructions": "Tidy it up."},
                                              {"name": "space", "prompt": "Disk is full."},
                                              {"instructions": "Be careful.", "model": "pro"}],
                            model="flash")
        tls = fork.parse_variants(p)
        self.assertEqual([t["name"] for t in tls], ["tidy", "space", "variant-3"])
        self.assertEqual([t["model"] for t in tls], ["deepseek/deepseek-v4-flash"] * 2 + ["deepseek/deepseek-v4-pro"])
        self.assertEqual(fork.prompt_for("Clean this.", tls[0]), "Clean this.\n\nTidy it up.")
        self.assertEqual(fork.prompt_for("Clean this.", tls[1]), "Disk is full.")
        with self.assertRaises(ValueError):
            fork.prompt_for(None, tls[0])
        tls = fork.parse_variants(F.variants_file("unit_variants_map", {"a": "one", "b": "two"}, model="x@y"))
        self.assertEqual([(t["name"], t["model"], t["provider"], t["instructions"]) for t in tls],
                         [("a", "x", "y", "one"), ("b", "x", "y", "two")])
        with self.assertRaises(ValueError):
            fork.parse_variants(F.variants_file("unit_variants_empty", [], model="flash"))


class _Proc:
    def __init__(self, rc):
        self.returncode = rc


class _FakeChild:
    def __init__(self, d, name, rc, out="", err="", calls=()):
        self.t = {"name": name, "model": "nousresearch/hermes-4-405b", "provider": None}
        base = os.path.join(d, name)
        self.out_path, self.err_path, self.calls_path = base + ".out.json", base + ".err.txt", base + ".calls.jsonl"
        for p, s in ((self.out_path, out), (self.err_path, err)):
            with open(p, "w") as f:
                f.write(s)
        with open(self.calls_path, "w") as f:
            f.write("".join(json.dumps(c) + "\n" for c in calls))
        self.proc, self.t0, self.t1, self.timed_out = _Proc(rc), 0.0, 6.1, False
        self.env = {fork.ENV_MAX_CALLS: "25"}


def _meta(rid, ws, status="finished", **kw):
    return dict({"schema": names.META_SCHEMA, "id": rid, "workspace": ws, "status": status, "task": "t",
                 "model_calls": 3, "tool_calls": 3, "seconds": 1.0}, **kw)


def _file(i, path, change, sha="s", frm=None, irr=False):
    return {"id": i, "class": "file", "tool": "terminal", "change": change, "target": {"path": path},
            "from_path": frm, "summary": f"{change} {path}", "irreversible": irr, "default_selected": True,
            "apply": {"appliable": True}, "after": {"sha256": sha} if change != "deleted" else {}, "badges": []}


def _http(i, url, body):
    return {"id": i, "class": "http", "tool": "terminal", "target": {"url": url, "method": "POST"},
            "preview": {"request": {"method": "POST", "url": url, "body": body}}, "summary": f"POST {url}",
            "irreversible": True, "default_selected": True, "apply": {"appliable": True}, "badges": ["IRREVERSIBLE"]}


def _write_rehearsal(rid, ws, rows, **meta):
    d = os.path.join(os.environ["HERMES_HOME"], "plugin-data", names.PLUGIN_NAME, rid)
    os.makedirs(d)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump(_meta(rid, ws, **meta), f)
    with open(os.path.join(d, "ledger.jsonl"), "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


def _write_fork(fid, ws, timelines, **kw):
    d = os.path.join(os.environ["HERMES_HOME"], "plugin-data", names.PLUGIN_NAME, fork.FORKS_DIR, fid)
    os.makedirs(d)
    rec = dict({"schema": fork.FORK_SCHEMA, "id": fid, "task": "My disk is nearly full.", "workspace": ws,
                "status": "finished", "mode": "at once", "wall_seconds": 12.5, "timelines": timelines}, **kw)
    with open(os.path.join(d, "fork.json"), "w") as f:
        json.dump(rec, f)


class ForkDrop(unittest.TestCase):
    def test_cannot_drive_the_tool_loop_is_dropped(self):
        with _TmpHome() as home:
            c = _FakeChild(home, "hermes-4-405b", 2, err="hermes -z: agent failed: " + HERMES_405B + "\n")
            o = fork._outcome(c)
            self.assertEqual(o["status"], "dropped", o)
            self.assertIn("No endpoints found that support tool use", o["reason"])
            self.assertIsNone(o["rid"])

    def test_a_finished_run_is_not_dropped(self):
        with _TmpHome() as home:
            rid = names.RID_PREFIX + "20261110-000000-aaaa"
            _write_rehearsal(rid, "/Users/you/projects/x", [])
            c = _FakeChild(home, "ok", 0, out=json.dumps({"rehearsal": {"id": rid}}),
                           calls=[{"k": 1, "model": "nousresearch/hermes-4-405b", "capped": False}])
            o = fork._outcome(c)
            self.assertEqual((o["status"], o["rid"], o["model_calls"]), ("finished", rid, 1))
            self.assertNotIn("model_switched", o)

    def test_a_failed_run_says_why_not_the_banner(self):
        """Seen live: a timeline interrupted mid-call (the Mac slept) was reported with the 'rehearsing in'
        banner, the only stderr line, as its reason."""
        with _TmpHome() as home:
            rid = names.RID_PREFIX + "20261110-000000-aaac"
            _write_rehearsal(rid, "/Users/you/projects/x", [], status="failed", exit_code=130,
                             agent_final_text="Operation interrupted: waiting for model response (656.8s elapsed).\n",
                             usage={"turn_exit_reason": "interrupted_during_api_call(session_turn_lease_lost)"})
            banner = f"{names.PRODUCT} {rid}: rehearsing in /Users/you/projects/x (nothing will happen for real)\n"
            c = _FakeChild(home, "tidy", 130, out=json.dumps({"rehearsal": {"id": rid}}), err=banner)
            o = fork._outcome(c)
            self.assertEqual(o["status"], "failed")
            self.assertNotIn("rehearsing in", o["reason"])
            self.assertIn("interrupted_during_api_call(session_turn_lease_lost)", o["reason"])
            self.assertIn("Operation interrupted: waiting for model response", o["reason"])
            self.assertIn("exit 130", o["reason"])

    def test_fallback_model_is_flagged(self):
        with _TmpHome() as home:
            rid = names.RID_PREFIX + "20261110-000000-aaab"
            _write_rehearsal(rid, "/Users/you/projects/x", [])
            c = _FakeChild(home, "fb", 0, out=json.dumps({"rehearsal": {"id": rid}}),
                           calls=[{"k": 1, "model": "nousresearch/hermes-4-405b"}, {"k": 2, "model": "other/model"}])
            self.assertEqual(fork._outcome(c)["model_switched"], ["nousresearch/hermes-4-405b", "other/model"])


class ForkCompare(unittest.TestCase):
    WS = "/Users/you/projects/Downloads"

    def _three(self, n_extra=0):
        rp = lambda k: names.RID_PREFIX + f"20261110-1015{k:02d}-ab{k:02d}"  # noqa: E731
        url = "http://127.0.0.1:19140/summary"
        common = [_file(i + 1, f"Screenshots/shot_{i}.png", "renamed", frm=f"shot_{i}.png") for i in range(n_extra)]
        k = len(common)
        a = common + [_file(k + 1, "notes.txt", "modified", sha="A"), _http(k + 2, url, "flash summary")]
        b = common + [_file(k + 1 + j, f"keep/f{j}.pdf", "deleted") for j in range(9)] + \
            [_file(k + 10, "notes.txt", "modified", sha="B"), _http(k + 11, url, "pro summary")]
        c = common + [_file(k + 1 + j, f"Archive/f{j}.pdf", "renamed", frm=f"keep/f{j}.pdf") for j in range(4)] + \
            [_http(k + 5, url, "kimi summary"), _http(k + 6, url, "kimi retry")]
        tls = []
        for i, (name, rows) in enumerate((("deepseek-v4-flash", a), ("deepseek-v4-pro", b), ("kimi-k2.6", c))):
            _write_rehearsal(rp(i), self.WS, rows)
            tls.append({"name": name, "model": "x/" + name, "provider": None, "rid": rp(i), "status": "finished",
                        "seconds": 10.0 + i, "model_calls": 5 + i, "cost_usd": 0.01 * (i + 1)})
        tls.append({"name": "hermes-4-405b", "model": "nousresearch/hermes-4-405b", "rid": None, "status": "dropped",
                    "reason": "cannot drive the tool loop: " + HERMES_405B, "seconds": 6.1})
        fid = fork.FORK_PREFIX + "20261110-101500-abcd"
        _write_fork(fid, self.WS, tls)
        return fid

    def test_differences_in_plain_words(self):
        with _TmpHome():
            fid = self._three(n_extra=3)
            data = compare.build(fid)
            texts = [d["text"] for d in data["differences"]]
            self.assertEqual(texts[0], "only in deepseek-v4-pro: delete 9 files in keep/: f0.pdf, f1.pdf, f2.pdf +6", texts)
            self.assertIn("only in kimi-k2.6: move 4 files into Archive/", texts)
            self.assertIn("not in kimi-k2.6: modify notes.txt, different content", texts)
            self.assertIn("all: POST http://127.0.0.1:19140/summary (deepseek-v4-flash 1×, deepseek-v4-pro 1×, "
                          "kimi-k2.6 2×)", texts)
            self.assertEqual(data["identical_in_all"], 3)
            self.assertEqual(data["compared"], ["deepseek-v4-flash", "deepseek-v4-pro", "kimi-k2.6"])
            tl = {t["name"]: t for t in data["timelines"]}
            self.assertEqual(tl["deepseek-v4-pro"]["tally"]["irreversible"], 1)
            self.assertEqual(tl["deepseek-v4-pro"]["file_verbs"], {"move": 3, "delete": 9, "modify": 1})
            js = compare.to_json(data)
            json.dumps(js)  # serialisable
            self.assertNotIn("rows", js["timelines"][0])

    def test_long_command_is_cut(self):
        """Seen live: a timeline's one-line `ls ...; curl ... --data '<long summary>'` filled 14 lines of the
        comparison."""
        with _TmpHome():
            rp = names.RID_PREFIX + "20261110-101700-ab0"
            cmd = "cd /Users/you/projects/Downloads && ls Misc; curl -s -X POST http://127.0.0.1:19140/summary " \
                  "--data '" + "a long summary of everything " * 12 + "'"
            row = {"id": 1, "class": "command", "tool": "terminal", "target": {"command": cmd}, "summary": "run",
                   "irreversible": True, "previewable": False, "default_selected": False, "apply": {"appliable": True},
                   "badges": ["CAN'T PREVIEW"]}
            _write_rehearsal(rp + "1", self.WS, [row])
            _write_rehearsal(rp + "2", self.WS, [])
            fid = fork.FORK_PREFIX + "20261110-101700-abcf"
            _write_fork(fid, self.WS, [{"name": "a", "rid": rp + "1", "status": "finished"},
                                       {"name": "b", "rid": rp + "2", "status": "finished"}])
            data = compare.build(fid)
            self.assertLessEqual(len(data["differences"][0]["text"]), 130)
            self.assertTrue(data["differences"][0]["text"].startswith("only in a: run `cd /Users/you/projects"))
            self.assertLessEqual(len(compare.render_text(data, color=False).splitlines()), 30)

    def test_same_target_different_content(self):
        with _TmpHome():
            rp = names.RID_PREFIX + "20261110-101600-ab0"
            _write_rehearsal(rp + "1", self.WS, [_file(1, "notes.txt", "modified", sha="A")])
            _write_rehearsal(rp + "2", self.WS, [_file(1, "notes.txt", "modified", sha="B")])
            fid = fork.FORK_PREFIX + "20261110-101600-abce"
            _write_fork(fid, self.WS, [{"name": "a", "rid": rp + "1", "status": "finished"},
                                       {"name": "b", "rid": rp + "2", "status": "finished"}])
            texts = [d["text"] for d in compare.build(fid)["differences"]]
            self.assertEqual(texts, ["all: modify notes.txt, different content"])

    def test_rendering_contract(self):
        with _TmpHome():
            fid = self._three(n_extra=2)
            data = compare.build(fid)
            for w in (None, 72):
                text = compare.render_text(data, width=w, color=False)
                lim = w or 58
                self.assertEqual([ln for ln in text.splitlines() if report.vwidth(ln) > lim], [], f"width {lim}")
            # narrower than a fork id plus a timeline name: only those command arguments overflow (a command
            # is never cut; it continues on the next line)
            narrow = compare.render_text(data, width=40, color=False)
            over = [ln for ln in narrow.splitlines() if report.vwidth(ln) > 40]
            self.assertEqual(over, [f"    {fid}/deepseek-v4-flash", f"    {fid}/deepseek-v4-pro"])
            plain = compare.render_text(data, color=False)
            self.assertNotIn("\033[", plain)
            self.assertIn("only in deepseek-v4-pro: delete 9 files in keep/: f0.pdf, f1.pdf, f2.pdf +6",
                          " ".join(plain.split()))
            self.assertIn("dropped: cannot drive the tool loop", plain)
            self.assertIn("dropped (cannot drive the tool loop): hermes-4-405b", plain)
            self.assertIn(f"hermes {names.CLI_NAME} apply \\\n    {fid}/deepseek-v4-pro\n", plain)
            self.assertIn(f"hermes {names.CLI_NAME} apply {fid}/deepseek-v4-pro\n",
                          compare.render_text(data, width=72, color=False))
            self.assertNotIn(f"apply {fid}/hermes-4-405b", plain)
            col = compare.render_text(data, color=True)
            self.assertIn(AC.BADGE, col)
            self.assertEqual(AC.check(self, col), [], "red before any timeline is kept")
            self.assertNotIn(compare.KEEP, col)
            stripped = report._strip(col)
            self.assertEqual([ln for ln in stripped.splitlines() if report.vwidth(ln) > 58], [])
            # default colour decision: no TTY -> plain; NO_COLOR -> plain
            buf = io.StringIO()
            old = os.environ.pop("NO_COLOR", None)
            try:
                compare.print_compare(fid, stream=buf)
                self.assertNotIn("\033[", buf.getvalue())
            finally:
                if old is not None:
                    os.environ["NO_COLOR"] = old

    def test_kept_timeline_is_the_red_thread(self):
        """Once a timeline is applied: its block red with "● keep", the other blocks dim, the badge in reverse
        video inside both, and no red anywhere else."""
        with _TmpHome():
            fid = self._three(n_extra=2)
            p = os.path.join(os.environ["HERMES_HOME"], "plugin-data", names.PLUGIN_NAME, fork.FORKS_DIR, fid,
                             "fork.json")
            with open(p) as f:
                rec = json.load(f)
            rec["applied"] = [{"timeline": "deepseek-v4-pro", "applied": True, "status": "applied"}]
            with open(p, "w") as f:
                json.dump(rec, f)
            col = compare.render_text(compare.build(fid), color=True)
            runs = AC.check(self, col, red=[r".+"])
            self.assertIn("deepseek-v4-pro  ● keep", runs)
            red, dim = (False, False, True, False), (False, True, False, False)
            blocks, cur = {}, None
            for plain, sts in AC.styled_lines(col):
                if not plain:
                    cur = None
                elif not plain.startswith(" "):
                    cur = plain.split()[0]
                if red in sts:
                    self.assertEqual(cur, "deepseek-v4-pro", f"red outside the kept block: {plain!r}")
                if cur in ("deepseek-v4-flash", "deepseek-v4-pro", "kimi-k2.6"):
                    blocks.setdefault(cur, set()).update(sts)
            self.assertEqual(blocks["deepseek-v4-pro"], {red}, "the kept block is red and only red")
            self.assertEqual(blocks["deepseek-v4-flash"], {dim})
            self.assertEqual(blocks["kimi-k2.6"], {dim})
            self.assertEqual(col.count(AC.BADGE), 3, "the badge in every block, red or dim")
            plain = compare.render_text(compare.build(fid), color=False)
            self.assertIn("deepseek-v4-pro  ● keep", plain)
            self.assertEqual([ln for ln in plain.splitlines() if report.vwidth(ln) > 58], [])

    def test_renders_in_under_a_second(self):
        with _TmpHome():
            fid = self._three(n_extra=400)
            t0 = time.monotonic()
            data = compare.build(fid)
            compare.render_text(data, color=False)
            compare.render_text(data, color=True, expand=True)
            secs = time.monotonic() - t0
            F.dump("unit_render_time", {"effects_per_timeline": 400, "seconds": round(secs, 4)})
            self.assertLess(secs, 1.0)


class DemoForkCounts(unittest.TestCase):
    """`compare` on the demo's own fork: a capped recording's last call is answered by the demo itself ("cap
    stop" in its replay log), so the comparison counts it the way the demo screen does, not as a model call.
    Seen: `compare` printed 6 model calls for claude-sonnet where the demo showed 5. Stored counts stay."""

    def _demo_fork(self, home, provider):
        base = os.path.realpath(tempfile.mkdtemp(prefix="demo-base-", dir=home))
        ws = os.path.join(base, "you", "Downloads")
        replay = os.path.join(base, "stage", "replay")
        os.makedirs(ws)
        os.makedirs(replay)
        with open(os.path.join(base, "stage", "plan.json"), "w") as f:
            json.dump({"workspace": ws, "base": base}, f)
        logs = {"capped": [{"served": "recorded", "match": True}] * 5 + [{"served": "cap stop"}],
                "uncapped": [{"served": "recorded", "match": True}] * 6}
        tls = []
        for i, (name, rows) in enumerate(logs.items()):
            with open(os.path.join(replay, name + ".replay.jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))
            rid = names.RID_PREFIX + f"20261110-165844-00a{i}"
            _write_rehearsal(rid, ws, [_file(1, f"{name}.txt", "created")])
            tls.append({"name": name, "model": name, "provider": provider, "rid": rid, "status": "finished",
                        "model_calls": 6, "capped_calls": 0, "cap": 25, "seconds": 3.9, "cost_usd": 0.0})
        fid = fork.FORK_PREFIX + "20261110-165843-28ae"
        _write_fork(fid, ws, tls)
        return fid, os.path.join(os.environ["HERMES_HOME"], "plugin-data", names.PLUGIN_NAME, fork.FORKS_DIR, fid,
                                 "fork.json")

    @staticmethod
    def _calls(text):
        out, cur = {}, None
        for ln in text.splitlines():
            if ln and not ln.startswith(" "):
                cur = ln.split()[0]
            elif "model call" in ln and cur:
                out[cur] = ln.split()[0]
        return out

    def test_compare_counts_the_demo_cap_stop_as_the_demo_does(self):
        demo = importlib.import_module(pkg.__name__ + ".demo")
        with _TmpHome() as home:
            fid, fj = self._demo_fork(home, demo.REPLAY_PROVIDER)
            with open(fj, "rb") as f:
                before = f.read()
            buf = io.StringIO()
            self.assertEqual(compare.print_compare(fid, stream=buf), 0)
            self.assertEqual(self._calls(buf.getvalue()), {"capped": "5", "uncapped": "6"}, buf.getvalue())
            js = io.StringIO()
            compare.print_compare(fid, as_json=True, stream=js)
            tl = {t["name"]: t for t in json.loads(js.getvalue())["timelines"]}
            self.assertEqual((tl["capped"]["model_calls"], tl["uncapped"]["model_calls"]), (5, 6))
            with open(fj, "rb") as f:
                self.assertEqual(f.read(), before, "fork.json changed")
            # the demo's own screen (build + its shown_counts + render_text) is drawn exactly as before
            raw = compare.render_text(compare.build(fid), color=False)
            self.assertEqual(self._calls(raw), {"capped": "6", "uncapped": "6"})
            base = os.path.dirname(os.path.dirname(json.loads(before)["workspace"]))
            per = demo._replay_stats(os.path.join(base, "stage", "replay"))["per_timeline"]
            shown = compare.render_text(demo.shown_counts(compare.build(fid), per), color=False)
            self.assertEqual(self._calls(shown), {"capped": "5", "uncapped": "6"})

    def test_a_live_fork_keeps_its_counts(self):
        with _TmpHome() as home:
            fid, _ = self._demo_fork(home, None)
            buf = io.StringIO()
            compare.print_compare(fid, stream=buf)
            self.assertEqual(self._calls(buf.getvalue()), {"capped": "6", "uncapped": "6"})


CAP = r'''
import json, os, sys
import helpers_capture as C
pkg = C.load_plugin_package()
import importlib
fork = importlib.import_module(pkg.__name__ + ".fork")
fork._cap_state.update(cap=2, log=os.environ["CAP_LOG"], installed=True)
called = []
def next_call(req):
    called.append(req)
    return {"real": len(called)}
out = []
for k in range(4):
    r = fork.cap_middleware(next_call=next_call, request={"k": k}, model="m/x", provider="p", api_mode="chat_completions")
    out.append(r if isinstance(r, dict) else {"synthetic": type(r).__name__, "finish": r.choices[0].finish_reason,
                                              "text": r.choices[0].message.content})
if os.environ.get("CAP_BREAK"):
    def boom(*a, **k):
        raise RuntimeError("no reply")
    fork._synthetic_stop = boom
    print(json.dumps({"before_break": len(called)}), flush=True)
    fork.cap_middleware(next_call=next_call, request={"k": 9}, model="m/x")
    print(json.dumps({"returned": True, "called": len(called)}), flush=True)
print(json.dumps({"called": len(called), "out": out}), flush=True)
'''


PIN = r'''
import json, os, sys
import helpers_capture as C
pkg = C.load_plugin_package()
import importlib
fork = importlib.import_module(pkg.__name__ + ".fork")
out = {"hermes_alone": list(fork._hermes_choice("moonshotai/kimi-k2.6")),
       "nvidia_creds": fork._has_credentials("nvidia"), "home_default": fork._home_default()}
for spec in ("moonshotai/kimi-k2.6", "kimi", "deepseek/deepseek-v4-flash"):
    t = fork.pin_provider(fork.parse_model(spec))
    out[spec] = [t["model"], t["provider"], t["provider_from"]]
print(json.dumps(out))
'''


class ForkProviderNoDefault(unittest.TestCase):
    def test_home_without_a_default_provider(self):
        """Hermes' real resolver in a home whose config has no model.provider, an OpenRouter key (a dummy) and
        no NVIDIA key; proxies point at a closed port, nothing leaves the machine and no model is called."""
        d = tempfile.mkdtemp(prefix="fork-nodefault-", dir=H.path("scripts"))
        with open(os.path.join(d, "config.yaml"), "w") as f:
            f.write("plugins:\n  enabled: []\n")
        dead = f"http://127.0.0.1:{H.TRAP_PORT}"
        env = {"HERMES_HOME": d, "OPENROUTER_API_KEY": "sk-or-dummy-for-a-test", "HTTPS_PROXY": dead,
               "HTTP_PROXY": dead, "ALL_PROXY": dead, "NVIDIA_API_KEY": "", "HERMES_INFERENCE_PROVIDER": ""}
        try:
            r = F.run_in_home_python(PIN, **env)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            res = json.loads(r.stdout.strip().splitlines()[-1])
            F.dump("provider_no_default", res)
            self.assertIsNone(res["home_default"])
            self.assertEqual(res["hermes_alone"][0], "nvidia", "the condition this test is about no longer holds")
            self.assertFalse(res["nvidia_creds"])
            for spec in ("moonshotai/kimi-k2.6", "kimi"):
                self.assertEqual(res[spec][:2], ["moonshotai/kimi-k2.6", "openrouter"], res)
            self.assertNotEqual(res["deepseek/deepseek-v4-flash"][1], "nvidia", res)
        finally:
            H.move_aside(d)


class ForkCapUnit(unittest.TestCase):
    def test_cap_answers_over_cap_calls_itself(self):
        log = H.path("logs", "fork_cap_unit_calls.jsonl")
        r = F.run_in_home_python(CAP, CAP_LOG=log)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        res = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(res["called"], 2, "the provider was called over the cap")
        self.assertEqual(res["out"][:2], [{"real": 1}, {"real": 2}])
        for o in res["out"][2:]:
            self.assertEqual((o["synthetic"], o["finish"]), ("ChatCompletion", "stop"))
            self.assertIn("cap of 2 model calls", o["text"])
        rows = H.lines(log)
        self.assertEqual([x["capped"] for x in rows], [False, False, True, True])

    def test_no_stop_reply_ends_the_process_instead_of_calling_the_provider(self):
        log = H.path("logs", "fork_cap_unit_break.jsonl")
        r = F.run_in_home_python(CAP, CAP_LOG=log, CAP_BREAK="1")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr[-2000:])
        self.assertNotIn("returned", r.stdout)
        self.assertIn("no stop reply could be built", r.stderr)


if __name__ == "__main__":
    unittest.main()
