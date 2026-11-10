"""`hermes clotho demo` end to end, run the way someone trying it would: a fresh Hermes home with no .env,
every key-like variable unset, nothing but the command.

DemoRun       the demo finishes in under 90 s; the comparison shows the three timelines, all finished; Apply of
              the chosen timeline leaves the folder holding exactly the files of that timeline's clone, with 0
              model calls; every recorded call was answered from the recording and none reached the model
              endpoint; the proxy trap saw 0 connections and caught its test request; the network guard
              blocked nothing and caught its test connection in every demo process (stage + 3 timelines); no
              key variable reached the demo's processes; an observer outside the demo (lsof over its whole
              process tree, every 0.1 s) saw no socket to anything but 127.0.0.1/::1, and saw its own test
              connection; afterwards, in the demo's own home, `compare` gives each timeline its recorded number
              of model calls and a bare `apply` (also `--dry-run`) names the timelines and changes nothing
DemoColour    the same command on a colour terminal (FORCE_COLOR): the colour contract over the whole output; in
              "what each timeline would do to the precious files", printed before Apply, the timeline that will
              be kept is red with "● keep" and the other two are dim; the only other red is the receipt's
              "0 model calls"
DemoControl   the same command with the recording withheld (test seam): the timelines' model calls reach the
              local model endpoint and are logged there, and the demo exits non-zero. This is the positive
              control for "0 model calls": the instrument sees a model call made through this exact path.
"""
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import unittest

import ansi_contract as AC
import harness as H
import names

LIMIT = 90.0
KEYS = re.compile(r"(API_KEY|_TOKEN|_SECRET|_PASSWORD|ACCESS_KEY)$|^(OPENROUTER|ANTHROPIC|OPENAI|NOUS|DEEPSEEK|"
                  r"MOONSHOT|GEMINI|GOOGLE|AZURE|AWS|MISTRAL|GROQ|XAI|TOGETHER|FIREWORKS|CLAUDE)_|^HERMES_INFERENCE_",
                  re.I)


def fresh_home(d: str) -> str:
    """A Hermes home that has never run anything: Hermes' runtime cloned (copy-on-write) from the test home,
    this checkout's plugin with its demo/ folder, no .env, no providers."""
    src = os.path.realpath(H.HOME)
    os.makedirs(d)
    d = os.path.realpath(d)
    for sub in ("installs", "source-checks"):
        if os.path.isdir(os.path.join(src, sub)):
            subprocess.run(["/bin/cp", "-c", "-R", os.path.join(src, sub), os.path.join(d, sub)], check=True)
    for p in glob.glob(os.path.join(d, "installs", "*", "facts.json")) + \
            glob.glob(os.path.join(d, "source-checks", "*.json")):
        with open(p) as f:
            s = f.read()
        with open(p, "w") as f:
            f.write(s.replace(src, d))
    plug = os.path.join(d, "plugins", names.PLUGIN_NAME)
    os.makedirs(plug)
    for f in os.listdir(H.REPO):
        if f == "plugin.yaml" or f.endswith((".py", ".sb")):
            shutil.copy2(os.path.join(H.REPO, f), plug)
    shutil.copytree(os.path.join(H.REPO, "demo"), os.path.join(plug, "demo"))
    with open(os.path.join(d, "config.yaml"), "w") as f:
        f.write(f"plugins:\n  enabled:\n    - {names.PLUGIN_NAME}\n  disabled: []\n")
    return d


def keyless_env(home: str, color=False, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not KEYS.search(k)}
    env.update(HERMES_HOME=home, PYTHONDONTWRITEBYTECODE="1")
    if color:
        env.pop("NO_COLOR", None)
        env["FORCE_COLOR"] = "1"
    else:
        env["NO_COLOR"] = "1"
        env.pop("FORCE_COLOR", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


class Observer:
    """Lists the sockets of a process tree every 0.1 s with lsof (outside the demo, independent of its guard)."""

    def __init__(self, root_pid: int):
        self.root, self.seen, self.polls, self.stop_ = root_pid, set(), 0, threading.Event()
        self.th = threading.Thread(target=self._loop, daemon=True)

    def _tree(self) -> list:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
        kids = {}
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) == 2:
                kids.setdefault(int(parts[1]), []).append(int(parts[0]))
        tree, todo = [], [self.root]
        while todo:
            p = todo.pop()
            tree.append(p)
            todo.extend(kids.get(p, []))
        return tree

    def _loop(self):
        while not self.stop_.is_set():
            pids = self._tree()
            r = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-i", "-p", ",".join(map(str, pids))],
                               capture_output=True, text=True)
            for ln in r.stdout.splitlines()[1:]:
                self.seen.add(" ".join(ln.split()[7:]))
            self.polls += 1
            self.stop_.wait(0.1)

    def start(self):
        self.th.start()
        return self

    def stop(self):
        self.stop_.set()
        self.th.join(timeout=10)

    def outside(self) -> list:
        """Sockets with an endpoint that is not loopback (listening or connected)."""
        bad = []
        for s in self.seen:
            addrs = re.findall(r"(\[?[0-9a-fA-F:.*]+\]?):(\d+|\*)", s)
            for host, _port in addrs:
                h = host.strip("[]")
                if h not in ("127.0.0.1", "::1", "*", "localhost") and not h.startswith("127."):
                    bad.append(s)
        return bad


def run_demo(label: str, home: str, base: str, color=False, **extra):
    argv = H.HERMES_CMD + [names.CLI_NAME, "demo", "--dir", base, "--sink-port", str(H.SINK_PORT),
                           "--trap-port", str(H.TRAP_PORT), "--endpoint-port", str(H.STUB_PORT)]
    for p in (H.SINK_PORT, H.TRAP_PORT, H.STUB_PORT):
        if not H.port_free(p):
            raise RuntimeError(f"port {p} is taken; the demo test needs the three test ports free")
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, cwd=H.path("ws"), env=keyless_env(home, color, **extra), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    obs = Observer(proc.pid).start()
    out, err = proc.communicate(timeout=300)
    seconds = time.monotonic() - t0
    obs.stop()
    with open(H.path("logs", f"demo_{label}.out.txt"), "w") as f:
        f.write(f"$ {' '.join(argv)}\nexit {proc.returncode} in {seconds:.1f}s\n\n{out}")
    with open(H.path("logs", f"demo_{label}.err.txt"), "w") as f:
        f.write(err)
    summary = {}
    try:
        with open(os.path.join(base, "stage", "demo.json")) as f:
            summary = json.load(f)
    except (OSError, ValueError):
        pass
    return proc.returncode, out, err, seconds, summary, obs


def observer_control() -> bool:
    """The observer sees a socket a watched process holds open (a local listener, a held connection)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    child = subprocess.Popen([H.PY, "-c", "import socket,time; s=socket.create_connection(('127.0.0.1', %d)); "
                                          "time.sleep(1.5)" % port])
    obs = Observer(child.pid).start()
    child.wait(timeout=10)
    obs.stop()
    srv.close()
    return any(f":{port}" in s for s in obs.seen)


def files_of(root: str) -> dict:
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.isfile(p) and not os.path.islink(p):
                out[os.path.relpath(p, root)] = H.snapshot.sha256_file(p)
    return out


class DemoRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H.require_home()
        cls.home = fresh_home(H.path("demo_home"))
        cls.base = H.path("demo_run")
        cls.rc, cls.out, cls.err, cls.seconds, cls.s, cls.obs = run_demo("run", cls.home, cls.base)
        with open(H.path("logs", "demo_run_summary.json"), "w") as f:
            json.dump({"seconds": cls.seconds, "rc": cls.rc, "observer_polls": cls.obs.polls,
                       "observer_seen": sorted(cls.obs.seen), "summary": cls.s}, f, indent=1, default=str)

    def test_1_finishes_in_time_and_cleanly(self):
        self.assertEqual(self.rc, 0, self.err[-3000:] + self.out[-2000:])
        self.assertLess(self.seconds, LIMIT, f"the demo took {self.seconds:.1f} s")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".env")))

    def test_2_screen_names_the_recording(self):
        with open(os.path.join(H.REPO, "demo", "recordings", "manifest.json")) as f:
            man = json.load(f)
        self.assertIn("RECORDED MODEL TURNS", self.out)
        self.assertIn(man["recorded_on"], self.out)
        flat = " ".join(self.out.split())
        for t in man["timelines"]:
            self.assertIn(t["recorded_model"], flat)
        # the task is shown as the timelines get it: the sink placeholder filled in with this run's sink
        self.assertNotIn("{sink}", self.out)
        self.assertIn(f"as a POST request to http://127.0.0.1:{H.SINK_PORT}/summary (use curl)", flat)

    def test_3_comparison_shows_three_timelines(self):
        st = self.s["stage"]
        self.assertEqual(len(st["timelines"]), 3, st["timelines"])
        self.assertEqual({t["status"] for t in st["timelines"]}, {"finished"}, st["timelines"])
        self.assertIn("3 timelines · at once", self.out)
        for t in st["timelines"]:
            self.assertIn(t["name"], self.out)
        self.assertRegex(self.out, r"differences \(\d+\)")

    def test_3b_comparison_counts_the_recorded_calls(self):
        # each timeline's line in the comparison shows as many model calls as were recorded (manifest.json), the
        # same number the "recorded:" line below it gives; the stop a capped timeline gets at its cap is not one
        with open(os.path.join(H.REPO, "demo", "recordings", "manifest.json")) as f:
            man = json.load(f)
        start = self.out.rindex(f"{names.PRODUCT} fork  ")
        cmp = self.out[start:self.out.index("differences (", start)]
        for t in man["timelines"]:
            block = cmp.split("\n" + t["name"] + "\n", 1)[1].split("\n\n", 1)[0]
            self.assertRegex(block, rf"(?m)^  {t['calls']} model calls? · ", f"{t['name']}:\n{block}")
        self.assertIn(f"recorded: {man['timelines'][2]['calls']} model calls", self.out)

    def test_4_every_recorded_turn_replayed_and_matched(self):
        rp = self.s["stage"]["replay"]
        with open(os.path.join(H.REPO, "demo", "recordings", "manifest.json")) as f:
            man = json.load(f)
        self.assertEqual(rp["served"], sum(t["calls"] for t in man["timelines"]), rp)
        self.assertEqual(rp["matched"], rp["checked"], rp)
        self.assertEqual(rp["beyond"], 0, rp)

    def test_5_apply_lands_the_chosen_timeline(self):
        st = self.s["stage"]
        chosen = st["chosen"]
        self.assertTrue(chosen)
        t = next(x for x in st["timelines"] if x["name"] == chosen)
        self.assertEqual(st["apply_exit"], 0, st.get("receipt"))
        rc = st["receipt"]
        self.assertEqual(rc["model_calls"], 0)
        self.assertEqual(rc.get("failed", 0), 0, rc)
        data = os.path.join(self.base, "you", ".hermes", "plugin-data", names.PLUGIN_NAME)  # the demo's own home
        clone = os.path.join(data, t["rid"], "work")
        ws = os.path.join(self.base, "you", "Downloads")
        self.assertEqual(files_of(ws), files_of(clone), "the folder does not hold the chosen timeline's files")
        self.assertNotEqual(self.s["tree_before"], self.s["tree_after"])
        # the other timelines left no trace: their clones differ from the folder now
        for o in st["timelines"]:
            if o["name"] != chosen:
                other = os.path.join(data, o["rid"], "work")
                self.assertNotEqual(files_of(other), files_of(ws), o["name"])
        # what reached the sink is exactly the chosen timeline's applied web requests
        ledger = H.lines(os.path.join(data, t["rid"], "ledger.jsonl"))
        web = [r for r in ledger if r["class"] == "http"]
        self.assertEqual(len(self.s["sink"]), len(web), (self.s["sink"], web))

    def test_6_nothing_left_the_machine(self):
        s = self.s
        self.assertTrue(s["network_ok"])
        self.assertEqual([t for t in s["trap"] if not t.get("control")], [], "the proxy trap saw a connection")
        self.assertEqual(len([t for t in s["trap"] if t.get("control")]), 1, "control: the trap missed its test")
        self.assertEqual([e for e in s["endpoint"] if e.get("model_call")], [], "a model call reached the endpoint")
        self.assertEqual([g for g in s["guard"] if not g.get("control")], [], "the guard blocked something")
        self.assertGreaterEqual(s["guard_processes"], 4, "control: the guard's test connection was not caught in "
                                                        "the stage and all three timeline processes")
        self.assertEqual(s["stage"].get("key_vars_present"), [], "a key variable reached the demo's processes")

    def test_8_afterwards_in_the_demo_home(self):
        """Seen after a demo: `compare` gave claude-sonnet 6 model calls where the demo showed 5, and a bare
        `apply --dry-run` took the kimi-k2.6 timeline (its rehearsal id sorted last), not the kept one."""
        home = os.path.join(self.base, "you", ".hermes")  # the demo's own home
        ws = os.path.join(self.base, "you", "Downloads")
        env = keyless_env(home)

        def run(*args, label):
            argv = H.HERMES_CMD + [names.CLI_NAME, *args]
            p = subprocess.run(argv, cwd=ws, env=env, capture_output=True, text=True, timeout=120,
                               stdin=subprocess.DEVNULL)
            with open(H.path("logs", f"demo_after_{label}.txt"), "w") as f:
                f.write(f"$ {' '.join(argv)}\nexit {p.returncode}\n\n{p.stdout}\n--- stderr\n{p.stderr}")
            return p
        with open(os.path.join(H.REPO, "demo", "recordings", "manifest.json")) as f:
            man = json.load(f)
        c = run("compare", label="compare")
        self.assertEqual(c.returncode, 0, c.stderr[-1500:])
        for t in man["timelines"]:
            block = c.stdout.split("\n" + t["name"], 1)[1].split("\n\n", 1)[0]
            self.assertRegex(block, rf"(?m)^  {t['calls']} model calls? · ", f"{t['name']}:\n{block}")
        files0 = files_of(ws)
        data = os.path.join(home, "plugin-data", names.PLUGIN_NAME)
        receipts0 = sorted(glob.glob(os.path.join(data, "r-*", "receipts", "*")))
        fid = self.s["stage"]["fork"]
        for flags in ([], ["--dry-run"]):
            a = run("apply", *flags, label="apply" + "".join(flags).replace("-", "_"))
            self.assertEqual(a.returncode, 3, a.stdout[-1500:] + a.stderr[-1500:])
            self.assertEqual(a.stdout.strip(), "")
            joined = " ".join(a.stderr.split()).replace("\\ ", "")
            for t in man["timelines"]:
                self.assertIn(f"hermes {names.CLI_NAME} apply {fid}/{t['name']}", joined)
            self.assertIn("nothing was changed", a.stderr)
        self.assertEqual(files_of(ws), files0, "the folder changed")
        self.assertEqual(sorted(glob.glob(os.path.join(data, "r-*", "receipts", "*"))), receipts0)

    def test_7_independent_observer(self):
        self.assertTrue(observer_control(), "control: the lsof observer did not see a held connection")
        self.assertGreater(self.obs.polls, 10)
        self.assertEqual(self.obs.outside(), [], f"a non-loopback socket: {self.obs.outside()[:5]}")


class DemoColour(unittest.TestCase):
    PRECIOUS = "── what each timeline would do to the precious files"

    def test_kept_thread_is_red_before_apply(self):
        H.require_home()
        home = fresh_home(H.path("demo_home_colour"))
        rc, out, err, seconds, s, obs = run_demo("colour", home, H.path("demo_colour"), color=True)
        self.assertEqual(rc, 0, err[-3000:] + out[-2000:])
        chosen = s["stage"]["chosen"]
        runs = AC.check(self, out, red=[r".+"])
        red, dim = (False, False, True, False), (False, True, False, False)
        lines = AC.styled_lines(out)
        start = next(i for i, (p, _) in enumerate(lines) if p.startswith(self.PRECIOUS))
        apply_at = next(i for i, (p, _) in enumerate(lines) if p.startswith(f"── apply {chosen}"))
        end = next(i for i in range(start + 1, len(lines)) if not lines[i][0])
        self.assertLess(end, apply_at, "the precious block is printed before Apply")
        tl_names = {t["name"] for t in s["stage"]["timelines"]}
        names_seen, cur = [], None
        for plain, sts in lines[start + 1:end]:
            if plain.split()[0] in tl_names:
                cur = plain.split()[0]
                names_seen.append(plain)
            want = red if cur == chosen else dim
            self.assertEqual(sts, {want}, f"{cur}: {plain!r}")
        self.assertIn(f"{chosen}  ● keep", names_seen)
        self.assertEqual(len(names_seen), 3, names_seen)
        self.assertEqual([n for n in names_seen if "● keep" in n], [f"{chosen}  ● keep"])
        outside = [p for i, (p, sts) in enumerate(lines) if red in sts and not start < i < end]
        self.assertEqual(len(outside), 1, outside)
        self.assertIn("0 model calls", outside[0])
        self.assertIn("0 model calls", runs)
        self.assertIn(AC.BADGE, out, "the IRREVERSIBLE badge in the comparison")
        with open(H.path("logs", "demo_colour.raw.txt"), "w") as f:
            f.write(out)


class DemoControl(unittest.TestCase):
    def test_withheld_recording_reaches_the_endpoint(self):
        H.require_home()
        home = fresh_home(H.path("demo_home_control"))
        rc, out, err, seconds, s, obs = run_demo("control", home, H.path("demo_control"),
                                                 **{names.env("DEMO_TEST_NO_REPLAY"): "1"})
        calls = [e for e in s.get("endpoint", []) if e.get("model_call")]
        self.assertGreaterEqual(len(calls), 3, f"control: the endpoint logged no model call ({s.get('endpoint')})")
        self.assertNotEqual(rc, 0, "the demo reported success although model calls reached the endpoint")
        self.assertLess(seconds, 120, "a failed replay should end fast (the endpoint answers 400)")
        left = subprocess.run(["/bin/ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
        self.assertNotIn(H.path("demo_control"), left, "timeline processes outlived the demo")
        self.assertEqual([t for t in s.get("trap", []) if not t.get("control")], [])
        self.assertEqual(obs.outside(), [])


if __name__ == "__main__":
    unittest.main()
