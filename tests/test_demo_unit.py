"""The demo's parts without Hermes: the shipped recordings, path rewriting, tool-result hashes, the network
guard, the seeded folder.

DemoRecordings  demo/recordings: a manifest naming three timelines, each file present with a header and as
                many recorded calls as the manifest says; no path or name of the machine they were recorded
                on (only /Users/you/...), no earlier name of the plugin; missing files are refused with a
                plain message
DemoRewrite     record-time and replay-time rewriting are inverse on paths; a tool result hashes the same on
                another day, under another root, as a list of cached parts or as plain text, and differently
                when its content differs; the plugin's own notice in a captured call's result is left out of
                the hash (its wording is the plugin's, not the world's), the rest of that result is not
DemoObjects     provider responses survive the trip to JSON and back (namespaces, pydantic-like objects)
DemoGuard       the guard refuses and logs a connection to anything but 127.0.0.1/::1/localhost and a lookup
                of any other name, allows loopback, and logs its own test connection as a control
DemoSeed        the seeded folder: 99 files, the precious ones among them, ages counted back from now,
                file names that do not depend on the day
DemoApplyText   the apply output keeps its result lines when the dependency listing is dropped
"""
import getpass
import importlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import types
import unittest

import harness as H
import helpers_capture as C
from test_names import OLD_NAME

pkg = C.load_plugin_package()
demo = importlib.import_module(pkg.__name__ + ".demo")
REC = os.path.join(H.REPO, "demo", "recordings")


def _recording_files():
    with open(os.path.join(REC, "manifest.json")) as f:
        man = json.load(f)
    return man, [os.path.join(REC, t["file"]) for t in man["timelines"]]


class DemoRecordings(unittest.TestCase):
    def test_manifest_and_files(self):
        man, files = _recording_files()
        self.assertEqual(len(man["timelines"]), 3)
        self.assertRegex(man["recorded_on"], r"^\d{4}-\d{2}-\d{2}$")
        for t, p in zip(man["timelines"], files):
            head, rows = demo.load_recording(p)
            self.assertEqual(head.get("schema"), demo.RECORDING_SCHEMA, p)
            self.assertEqual(len(rows), t["calls"], p)
            self.assertGreater(len(rows), 0, p)
            self.assertEqual(head["model"], t["model"])
            for r in rows:
                self.assertIn("response", r)
                self.assertIsInstance(r.get("tool_hashes"), list)

    def test_no_trace_of_the_recording_machine(self):
        user = getpass.getuser()
        old = OLD_NAME
        for p in [os.path.join(REC, f) for f in os.listdir(REC)]:
            with open(p) as f:
                text = f.read()
            homes = set(re.findall(r"/Users/([A-Za-z0-9._-]+)", text))
            self.assertLessEqual(homes, {"you"}, f"{p}: home folders {homes}")
            self.assertNotIn("hermes-hackathon", text, p)
            self.assertNotIn("/private/var/folders", text, p)
            if len(user) > 2:
                self.assertNotIn(user, text, p)
            self.assertIsNone(old.search(text), f"{p}: old working name")

    def test_missing_recordings_are_refused_plainly(self):
        empty = tempfile.mkdtemp(prefix="demo-unit-norec-", dir=H.path("scripts") if H.HOME else None)
        old = os.environ.get(demo.names.env("DEMO_RECORDINGS"))
        os.environ[demo.names.env("DEMO_RECORDINGS")] = empty
        try:
            with self.assertRaises(demo.state.Refused) as cm:
                demo.manifest()
            self.assertIn("reinstall the plugin", str(cm.exception))
        finally:
            if old is None:
                os.environ.pop(demo.names.env("DEMO_RECORDINGS"), None)
            else:
                os.environ[demo.names.env("DEMO_RECORDINGS")] = old
            H.move_aside(empty)

    def test_task_names_the_sink_port(self):
        """Stored with a placeholder for the sink; a replay gives the models the URL they were given (the
        recorded port) or the run's own sink port."""
        man, _ = _recording_files()
        self.assertIn(f"{demo.SINK_TOKEN}/summary", man["task"])
        self.assertNotIn("http", man["task"])
        given = demo.rewrite(man["task"], demo.replay_pairs("/tmp/demo-x/you", demo.SINK_PORT))
        self.assertIn(f"POST request to http://127.0.0.1:{demo.SINK_PORT}/summary (use curl)", given)
        moved = demo.rewrite(man["task"], demo.replay_pairs("/tmp/demo-x/you", 50001))
        self.assertIn("http://127.0.0.1:50001/summary", moved)
        with open(os.path.join(demo.seed_dir(), "task.txt")) as f:
            self.assertEqual(f.read().strip(), man["task"])


class DemoRewrite(unittest.TestCase):
    def test_rewrites_are_inverse(self):
        ws, home, uh = "/Users/someone/proj/.run/x/Downloads", "/Users/someone/proj/.run/home", "/Users/someone"
        pairs = demo.record_pairs(ws, home, uh, "someone")
        rec = demo.rewrite({"a": [f"cd {ws} && ls", f"{home}/plugin-data", f"{uh}/.Trash"]}, pairs)
        self.assertEqual(rec, {"a": ["cd /Users/you/Downloads && ls", "/Users/you/.hermes/plugin-data",
                                     "/Users/you/.Trash"]})
        back = demo.rewrite(rec, demo.replay_pairs("/tmp/demo-x/you", 19207))
        self.assertEqual(back["a"][0], "cd /tmp/demo-x/you/Downloads && ls")
        moved = demo.rewrite("curl http://127.0.0.1:19207/summary", demo.replay_pairs("/r", 50001))
        self.assertEqual(moved, "curl http://127.0.0.1:50001/summary")

    def test_hash_ignores_dates_roots_and_cache_parts(self):
        rec_pairs = demo.record_pairs("/Users/someone/x/Downloads", "/Users/someone/h", "/Users/someone", "someone")
        a = '{"output": "-rw-r--r-- 1 someone staff 600 Nov 10 11:00 /Users/someone/x/Downloads/Q3.xlsx"}'
        b = '{"output": "-rw-r--r-- 1 you staff 600 Dec  2 09:13 /tmp/r/you/Downloads/Q3.xlsx"}'
        h1 = demo.text_hash(a, rec_pairs)
        h2 = demo.text_hash(b, [("/tmp/r/you", "/Users/you")])
        self.assertEqual(h1, h2)
        parts = [{"type": "text", "text": a, "cache_control": {"type": "ephemeral"}}]
        self.assertEqual(demo.text_hash(parts, rec_pairs), h1)
        self.assertNotEqual(demo.text_hash(a.replace("Q3.xlsx", "budget.xlsx"), rec_pairs), h1)

    def test_hash_leaves_out_the_plugins_own_notice(self):
        tag = demo.names.RESULT_TAG
        a = ('{"output": "%s captured: would POST http://127.0.0.1:19207/summary (241 bytes) - not executed. '
             'This is a rehearsal.", "exit_code": 0, "error": null}' % tag)
        b = ('{"output": "%s captured, not run: other wording of the same notice.", "exit_code": 0, '
             '"error": null}' % tag)
        self.assertEqual(demo.text_hash(a, []), demo.text_hash(b, []))
        self.assertNotEqual(demo.text_hash(a, []), demo.text_hash(a.replace('"error": null', '"error": "boom"'), []))
        plain = '{"output": "total 8\\nQ3.xlsx", "exit_code": 0, "error": null}'
        self.assertNotEqual(demo.text_hash(plain, []), demo.text_hash(plain.replace("Q3.xlsx", "budget.xlsx"), []))


class _Pyd:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)

    @classmethod
    def model_validate(cls, d):
        return cls(**d)


class DemoObjects(unittest.TestCase):
    def test_round_trip(self):
        import sys
        mod = types.ModuleType("demo_unit_pyd")
        mod._Pyd = _Pyd
        sys.modules["demo_unit_pyd"] = mod
        _Pyd.__module__ = "demo_unit_pyd"
        resp = types.SimpleNamespace(id="x", choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="hi", tool_calls=[{"function": {"name": "terminal"}}]))],
            usage=_Pyd(prompt_tokens=3, completion_tokens=4))
        back = demo.load_obj(json.loads(json.dumps(demo.dump_obj(resp))))
        self.assertIsInstance(back, types.SimpleNamespace)
        self.assertEqual(back.choices[0].message.content, "hi")
        self.assertEqual(back.choices[0].message.tool_calls[0]["function"]["name"], "terminal")
        self.assertIsInstance(back.usage, _Pyd)
        self.assertEqual(back.usage.completion_tokens, 4)


GUARD_PROBE = r'''
import socket, sys
res = {}
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
try:
    socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=2).close(); res["loopback"] = "ok"
except Exception as e:
    res["loopback"] = repr(e)
for host in ("10.255.255.1", "::2"):
    try:
        socket.getaddrinfo(host, 80); res[host] = "resolved"
    except PermissionError as e:
        res[host] = "blocked"
    except Exception as e:
        res[host] = repr(e)
try:
    socket.getaddrinfo("localhost", 80); res["localhost"] = "ok"
except Exception as e:
    res["localhost"] = repr(e)
print(repr(res))
'''


class DemoGuard(unittest.TestCase):
    def test_guard_blocks_and_logs(self):
        d = tempfile.mkdtemp(prefix="demo-guard-", dir=H.path("scripts") if H.HOME else None)
        log = os.path.join(d, "guard.jsonl")
        env = dict(os.environ, **{demo.ENV_GUARD: log})
        p = subprocess.run([H.PY, "-I", "-c", demo._GUARD + GUARD_PROBE], capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = eval(p.stdout.strip())  # noqa: S307 (our own repr)
        self.assertEqual(res["loopback"], "ok")
        self.assertEqual(res["localhost"], "ok")
        self.assertEqual(res["10.255.255.1"], "blocked")
        self.assertEqual(res["::2"], "blocked")
        rows = [json.loads(x) for x in open(log)]
        ctl = [r for r in rows if r["control"]]
        self.assertEqual(len(ctl), 1, rows)
        self.assertEqual(ctl[0]["addr"], "127.0.0.2:9")
        self.assertEqual(sorted(r["host"] for r in rows if not r["control"]), ["10.255.255.1", "::2"])

    def test_guard_off_without_its_variable(self):
        env = {k: v for k, v in os.environ.items() if k != demo.ENV_GUARD}
        p = subprocess.run([H.PY, "-I", "-c", demo._GUARD + "import sys; print(getattr(sys, '_rt_demo_guard', False))"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.stdout.strip(), "False", p.stderr)


class DemoSeed(unittest.TestCase):
    def test_seeded_folder(self):
        d = os.path.join(tempfile.mkdtemp(prefix="demo-seed-", dir=H.path("scripts") if H.HOME else None), "Downloads")
        now = time.time()
        n = demo.make_seed(d, now=now)
        self.assertEqual(n, 99)
        files = [os.path.relpath(os.path.join(dp, f), d) for dp, _, fn in os.walk(d) for f in fn]
        self.assertEqual(len(files), 99)
        for p in demo.precious():
            self.assertIn(p, files)
        ages = {x["path"]: x["age_days"] for x in demo.seed_files()}
        for rel in ("Docker.dmg", "tax_return_2024.pdf"):
            m = os.stat(os.path.join(d, rel)).st_mtime
            self.assertAlmostEqual((now - m) / 86400, ages[rel], places=3)
        self.assertTrue(any(f.startswith("Screenshot 2026-11-07 at ") for f in files), sorted(files)[:5])
        o = demo.outcome(d)
        self.assertEqual((o["lost"], o["gone"], len(o["precious_kept"])), (0, 0, len(demo.precious())))


class DemoShownCounts(unittest.TestCase):
    def test_cap_stop_is_not_shown_as_a_model_call(self):
        # a capped recording ends with the stop its live cap gave; in the demo that stop is answered by the demo
        # itself ("cap stop" in the replay log), not by a model, so the comparison shows the recorded count
        cmp = {"timelines": [{"name": "a", "model_calls": 6, "capped_calls": 0}, {"name": "b", "model_calls": 9},
                             {"name": "c", "model_calls": None}]}
        per = {"a": {"served": 5, "cap_stops": 1}, "b": {"served": 9, "cap_stops": 0}}
        out = demo.shown_counts(cmp, per)
        self.assertEqual([t["model_calls"] for t in out["timelines"]], [5, 9, None])
        self.assertEqual(out["timelines"][0]["capped_calls"], 1)
        self.assertNotIn("capped_calls", out["timelines"][1])

    def test_help_gives_the_measured_time(self):
        import argparse
        ap = argparse.ArgumentParser()
        demo.add_parser(ap.add_subparsers())
        text = " ".join(ap.format_help().split())
        for s in (text, " ".join(demo.__doc__.split())):
            self.assertNotIn("about a minute", s)
            self.assertIn("about 10 s", s)


if __name__ == "__main__":
    unittest.main()
