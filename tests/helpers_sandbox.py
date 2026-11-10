"""Fixtures for the sandbox tests: a workspace, its work clone, a private tmp and an outside folder.
Standard library only (sandbox.py and snapshot.py need nothing else)."""
from __future__ import annotations

import glob
import os
import sys
import time

import harness as H
import names
import sandbox
import snapshot


def venv_python() -> str:
    """The Python that Hermes' execute_code would use: the test home's venv, else this interpreter."""
    if H.HOME:
        hits = sorted(glob.glob(os.path.join(H.HOME, "installs", "*", "environments", "*", "venv", "bin", "python")))
        if hits:
            return hits[0]
    return sys.executable


class Case:
    """One scratch rehearsal: ws (the live workspace), work (its clone), tmp, outside, home overlay."""

    def __init__(self, label: str):
        self.base = H.path("sandbox", f"{label}-{time.time_ns()}")
        os.makedirs(self.base)
        self.ws = os.path.join(self.base, "ws")
        os.makedirs(self.ws)
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("a\n")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(self.outside)
        self.work = os.path.join(self.base, "work")
        snapshot.clonefile(self.ws, self.work)
        self.tmp = os.path.join(self.base, "tmp")
        self.home_overlay = os.path.join(self.base, "home-overlay")
        self.rid = f"case{time.time_ns()}"
        self.n = 0

    def tag(self) -> str:
        self.n += 1
        return f"{names.PLUGIN_NAME}-{self.rid}-{self.n}"

    def run(self, cmd, timeout=20, **kw):
        return sandbox.run(self.work, self.work, cmd, self.tmp, tag=self.tag(), timeout=timeout, orig=self.ws, **kw)


def reasons(res, cmd) -> list:
    return sandbox.reasons_cant_preview(res, sandbox.static_scan(cmd))


def listing(path: str):
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        return None
