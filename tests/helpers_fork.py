"""Helpers for the fork tests (tests/test_fork_*.py): start a fork through the real CLI, read its record,
write a variants file, and run plugin code under the home's own Python (the one that has openai).
Nothing here talks to the internet: every timeline runs on the scripted provider.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess

import harness as H
import names


def forks_root(home: str | None = None) -> str:
    return os.path.join(home or H.HOME, "plugin-data", names.PLUGIN_NAME, names.FORKS_DIR)


def fork_ids() -> set:
    d = forks_root()
    return set(os.listdir(d)) if os.path.isdir(d) else set()


def record(fid: str) -> dict:
    with open(os.path.join(forks_root(), fid, "fork.json")) as f:
        return json.load(f)


def variants_file(name: str, variants: list, **top) -> str:
    """A variants file (JSON is valid YAML) in this run's scripts folder."""
    p = H.path("scripts", f"{name}.yaml")
    with open(p, "w") as f:
        json.dump(dict(top, variants=variants), f, indent=1)
    return p


def fork(args: list, ws: str, label: str, timeout=400, **extra):
    """`hermes clotho fork ...` in ws; .fid is the new fork id (None when none was created)."""
    before = fork_ids()
    p = H.hermes([H.CLI, "fork", *args], cwd=ws, label=label, timeout=timeout, **extra)
    new = sorted(fork_ids() - before)
    p.fid = new[-1] if new else None
    return p


def compare(fid: str, ws: str, label: str, *flags, **extra):
    p = H.hermes([H.CLI, "compare", fid, *flags], cwd=ws, label=label, **extra)
    if "--json" in flags:
        try:
            p.data = json.loads(p.stdout)
        except ValueError:
            p.data = None
    return p


def apply(spec: str, ws: str, label: str, *flags, **extra):
    p = H.hermes([H.CLI, "apply", spec, "--json", *flags], cwd=ws, label=label, **extra)
    try:
        p.receipt = json.loads(p.stdout)
    except ValueError:
        p.receipt = None
    return p


def tools_requests() -> int:
    """Model requests the scripted provider answered with tools offered (titles and summaries excluded)."""
    return sum(1 for r in H.lines(H.STUB_LOG) if r.get("method") == "POST" and r.get("has_tools"))


def home_python() -> str:
    hits = sorted(glob.glob(os.path.join(H.HOME, "installs", "*", "environments", "*", "venv", "bin", "python")))
    if not hits:
        raise RuntimeError("the test home has no venv python under installs/")
    return hits[0]


def run_in_home_python(code: str, timeout=60, **env_extra) -> subprocess.CompletedProcess:
    """Run code with the home's own Python (it has openai and Hermes' dependencies), plugin importable via
    helpers_capture.load_plugin_package."""
    env = dict(os.environ, HERMES_TEST_HOME=H.HOME)
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run([home_python(), "-B", "-c", code], capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=os.path.join(H.REPO, "tests"))


def dump(label: str, obj) -> str:
    p = H.path("logs", f"fork_{label}.json")
    with open(p, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    return p
