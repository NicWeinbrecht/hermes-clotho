"""Drive the plugin's capture middleware in one Python process with Hermes' real tools: no agent loop, no
model, nothing leaves the machine.

A test writes a short driver (Python source) and runs it with ``run()``: the driver runs under the interpreter
Hermes itself uses, with Hermes' source on the path and HERMES_HOME pointed at a scratch home of the test's
own. Inside the driver, ``Session`` arms a rehearsal process-wide the way `hermes clotho run` does and sends
each call through ``capture.tool_execution`` with Hermes' registered handler as the next step of the chain
(``tools.registry.registry.dispatch``), so the plugin's own code and Hermes' real file, shell and skill tools
both run. ``finish()`` ends the rehearsal through ``cli.finish``, as `run` does when the model stops.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import harness as H
import helpers_sandbox as S

HERMES_SRC = os.environ.get("HERMES_SRC") or os.path.expanduser("~/.hermes/hermes-agent")
PKG = "rt_plugin_under_test"


def hermes_python() -> str:
    """The interpreter of HERMES_CMD when it names one, else the test home's dependency venv."""
    cmd = H.HERMES_CMD
    if cmd and os.path.basename(cmd[0]).startswith("python"):
        return cmd[0]
    return S.venv_python()


def scratch_home(label: str) -> str:
    """An empty Hermes home of this test's own (plugin data, skills, usage counters)."""
    home = H.path("homes", f"{label}-{time.time_ns()}")
    os.makedirs(home)
    return os.path.realpath(home)


def run(code: str, home: str, timeout=240, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(HERMES_HOME=home, HERMES_TEST_HOME=H.HOME, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([HERMES_SRC, os.path.join(H.REPO, "tests")]))
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run([hermes_python(), "-B", "-c", code], capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=os.path.join(H.REPO, "tests"))


def result(p: subprocess.CompletedProcess) -> dict:
    """The JSON object the driver printed last, or the driver's error output."""
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"driver_error": (p.stderr or p.stdout)[-3000:], "exit": p.returncode}


# ---------------------------------------------------------------- used inside a driver
def modules():
    import helpers_capture as C
    C.load_plugin_package(PKG)
    return {m: sys.modules[f"{PKG}.{m}"] for m in ("state", "capture", "cli", "apply", "report", "names", "snapshot")}


class Session:
    """One armed rehearsal of ``ws`` in this process."""

    def __init__(self, ws: str, task: str = "in-process test", task_id: str = "inproc-task"):
        self.m = modules()
        import model_tools  # noqa: F401  (registers Hermes' built-in tools)
        from tools.registry import registry
        self.registry = registry
        st = self.m["state"]
        self.r = st.Rehearsal(st.new_id(), ws, task=task, mode="run")
        self.r.create()
        st.arm_process(self.r)
        os.chdir(ws)
        os.environ["TERMINAL_CWD"] = ws
        self.task_id = task_id
        self.n = 0

    def call(self, tool: str, before_real=None, **args):
        """One tool call through the plugin's middleware; ``before_real`` runs just before Hermes' real
        handler would (for a test that needs something to happen during the call)."""
        self.n += 1

        def next_call(a):
            if before_real is not None:
                before_real()
            return self.registry.dispatch(tool, a, task_id=self.task_id, session_id="inproc-session")

        return self.m["capture"].tool_execution(tool_name=tool, args=args, next_call=next_call,
                                                tool_call_id=f"inproc-{self.n}", task_id=self.task_id,
                                                session_id="inproc-session")

    def finish(self) -> list:
        self.m["cli"].finish(self.r, 0, os.path.join(self.r.dir, "usage.json"), 0.1)
        return _ledger(self.r.ledger_path)


def _ledger(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]
