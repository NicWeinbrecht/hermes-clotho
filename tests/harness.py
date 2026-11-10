"""Shared test plumbing: a scratch Hermes home, a local HTTP sink, a scripted provider, workspaces.

Nothing here talks to the internet. The scripted provider is a fixture that replays fixed tool
calls (no model, no inference), so the plugin is driven through Hermes' real agent loop, tool
registry and middleware chain without spending anything.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import names  # noqa: E402  (every user-visible name of the plugin)
import snapshot  # noqa: E402  (stdlib-only module of the plugin)

# HERMES_CMD: how to start Hermes (space-separated), default the stock launcher. The stock launcher runs
# python -I, which ignores PYTHONDONTWRITEBYTECODE; tests/run.sh points HERMES_CMD at the home's own venv
# python with -B so test runs do not write bytecode into the Hermes install.
HERMES_CMD = os.environ.get("HERMES_CMD", "").split() or [os.path.expanduser("~/.local/bin/hermes")]
HOME = os.environ.get("HERMES_TEST_HOME", "")
CLI = names.CLI_NAME
STUB_PROVIDER = "scriptstub"  # provider name tests/run.sh writes into the test home
TESTKIT = "testkit"  # test-only plugin in fixtures/testkit
SINK_PORT = int(os.environ.get(names.env("TEST_SINK_PORT"), "18940"))
STUB_PORT = int(os.environ.get(names.env("TEST_STUB_PORT"), "18941"))
TRAP_PORT = int(os.environ.get(names.env("TEST_TRAP_PORT"), "18942"))
PY = sys.executable
FIX = os.path.join(REPO, "tests", "fixtures")


def require_home():
    if not HOME or not os.path.isdir(HOME):
        raise RuntimeError("HERMES_TEST_HOME must point at a scratch Hermes home (see tests/README.md)")
    real = os.path.realpath(os.path.expanduser("~/.hermes"))
    if os.path.realpath(HOME) == real:
        raise RuntimeError("refusing to test against ~/.hermes")


ROOT = None  # per-run folder next to the home: workspaces, scripts, logs


def root() -> str:
    global ROOT
    if ROOT is None:
        base = os.environ.get(names.env("TEST_ROOT")) or os.path.join(os.path.dirname(os.path.realpath(HOME)),
                                                                names.PLUGIN_NAME + "-tests")
        ROOT = os.path.join(base, time.strftime("%Y%m%d-%H%M%S"))
        for d in ("ws", "scripts", "logs", "sentinel", "trash"):
            os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    return ROOT


def path(*p) -> str:
    return os.path.join(root(), *p)


# ---------------------------------------------------------------- servers
def port_free(port: int) -> bool:
    r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    return not r.stdout.strip()


class Server:
    def __init__(self, name, argv, port):
        self.name, self.argv, self.port = name, argv, port
        self.pidfile = path("logs", f"{name}.pid")
        self.proc = None

    def start(self):
        if not port_free(self.port):
            raise RuntimeError(f"port {self.port} is taken by another process; set {names.env('TEST_*_PORT')}")
        self.proc = subprocess.Popen(self.argv + [self.pidfile], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(self.pidfile) and not port_free(self.port):
                break
            time.sleep(0.1)
        with open(self.pidfile) as f:
            pid = int(f.read())
        if pid != self.proc.pid:
            raise RuntimeError(f"{self.name}: pid file does not match the process started")
        # make sure the listener on the port is ours
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
        if str(pid) not in r.stdout.split():
            raise RuntimeError(f"{self.name}: port {self.port} is not held by pid {pid}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGTERM)  # by the pid we started, never by pattern
            self.proc.wait(timeout=5)


SINK_LOG = None
STUB_LOG = None
TRAP_LOG = None
_servers = []


def start_servers():
    global SINK_LOG, STUB_LOG, TRAP_LOG
    SINK_LOG, STUB_LOG, TRAP_LOG = path("logs", "sink.jsonl"), path("logs", "stub.jsonl"), path("logs", "trap.jsonl")
    for f in (SINK_LOG, STUB_LOG, TRAP_LOG):
        open(f, "a").close()
    s = [Server("sink", [PY, "-I", os.path.join(FIX, "sink.py"), str(SINK_PORT), SINK_LOG], SINK_PORT),
         Server("stub", [PY, "-I", os.path.join(FIX, "scripted_provider.py"), str(STUB_PORT), path("scripts"), STUB_LOG],
                STUB_PORT),
         Server("trap", [PY, "-I", os.path.join(FIX, "sink.py"), str(TRAP_PORT), TRAP_LOG], TRAP_PORT)]
    for srv in s:
        srv.start()
        _servers.append(srv)


def stop_servers():
    for srv in _servers:
        try:
            srv.stop()
        except Exception:
            pass


def lines(p) -> list:
    if not p or not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(x) for x in f if x.strip()]


def sink_requests(prefix=None) -> list:
    return [r for r in lines(SINK_LOG) if prefix is None or r["path"].startswith(prefix)]


def sink_with(text: str) -> list:
    """Requests whose body carries this test's own marker (the sink is shared by every test)."""
    return [r for r in lines(SINK_LOG) if text in (r.get("body") or "")]


def sink_selftest():
    """Positive control: the sink logs a request we send it directly."""
    n = len(sink_requests("/selftest"))
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{SINK_PORT}/selftest", data=b"x", method="POST"),
                           timeout=5).read()
    return len(sink_requests("/selftest")) == n + 1


# ---------------------------------------------------------------- scripts and workspaces
def script(name: str, steps: list, final: str = "Done.") -> str:
    with open(path("scripts", name + ".json"), "w") as f:
        json.dump({"steps": steps, "final": final}, f)
    return f"[script:{name}]"


def call(_tool, **arguments):
    return {"name": _tool, "arguments": arguments}


def make_ws(name: str, files: dict) -> str:
    d = path("ws", name)
    os.makedirs(d)
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return os.path.realpath(d)


def clone(src: str, dst: str) -> str:
    snapshot.clonefile(src, dst)
    return dst


def move_aside(p: str) -> str:
    dest = path("trash", os.path.basename(p) + "-" + str(int(time.time() * 1000)))
    shutil.move(p, dest)
    return dest


def read(p: str) -> str:
    with open(p) as f:
        return f.read()


def tree(p: str) -> str:
    return snapshot.tree_hash(p)


# ---------------------------------------------------------------- running hermes
def env(**extra) -> dict:
    e = dict(os.environ)
    e.update(HERMES_HOME=HOME, PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1",
             TESTKIT_SINK_PORT=str(SINK_PORT), TESTKIT_SENTINEL_DIR=path("sentinel"))
    e.pop("FORCE_COLOR", None)
    e.update({k: str(v) for k, v in extra.items()})
    return e


_log_n = [0]


def hermes(args: list, cwd: str, label: str, timeout=240, wrap=None, **extra) -> subprocess.CompletedProcess:
    _log_n[0] += 1
    argv = (wrap or []) + HERMES_CMD + args
    t0 = time.monotonic()
    p = subprocess.run(argv, cwd=cwd, env=env(**extra), capture_output=True, text=True, timeout=timeout)
    p.seconds = time.monotonic() - t0
    base = path("logs", f"{_log_n[0]:02d}_{label}")
    with open(base + ".out.txt", "w") as f:
        f.write(f"$ {' '.join(argv)}\n(cwd {cwd}) exit {p.returncode} in {p.seconds:.1f}s\n\n{p.stdout}")
    with open(base + ".err.txt", "w") as f:
        f.write(p.stderr)
    p.log = base
    return p


def rehearse(task_marker: str, ws: str, label: str, task="do the task", **extra):
    p = hermes([CLI, "run", f"{task_marker} {task}", "-m", "scripted", "--provider", STUB_PROVIDER, "--no-diffs"],
               cwd=ws, label=label, **extra)
    m = re.search(r"(r-\d{8}-\d{6}-[0-9a-f]{4})", p.stderr + p.stdout)
    p.rid = m.group(1) if m else None
    return p


def plain_run(task_marker: str, ws: str, label: str, task="do the task", **extra):
    return hermes(["-z", f"{task_marker} {task}", "-m", "scripted", "--provider", STUB_PROVIDER], cwd=ws, label=label,
                  **extra)


def rdir(rid: str) -> str:
    return os.path.join(HOME, "plugin-data", names.PLUGIN_NAME, rid)


def ledger(rid: str) -> list:
    return lines(os.path.join(rdir(rid), "ledger.jsonl"))


def meta(rid: str) -> dict:
    with open(os.path.join(rdir(rid), "meta.json")) as f:
        return json.load(f)


def rehearsal_ids() -> set:
    """Rehearsal ids in the home (forks/, cron-runs/ and other folders there are not rehearsals)."""
    d = os.path.join(HOME, "plugin-data", names.PLUGIN_NAME)
    return {x for x in os.listdir(d) if x.startswith(names.RID_PREFIX)} if os.path.isdir(d) else set()


def apply(rid: str, ws: str, label: str, *flags, wrap=None, **extra):
    p = hermes([CLI, "apply", rid, "--json", *flags], cwd=ws, label=label, wrap=wrap, **extra)
    try:
        p.receipt = json.loads(p.stdout)
    except ValueError:
        p.receipt = None
    return p
