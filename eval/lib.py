"""Plumbing for the evaluation runner: local servers, filesystem hashes, Hermes invocation, ledger scoring.

Stdlib only. Imports nothing from Hermes and nothing from the plugin except names.py (every
user-visible name of the plugin lives there).
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time

EVAL = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(EVAL)
sys.path.insert(0, REPO)
import names  # noqa: E402

PY = sys.executable


# ------------------------------------------------------------------------------------------------ servers
def port_free(port: int) -> bool:
    r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    return not r.stdout.strip()


class Server:
    """A local server started by us; stopped by the pid it wrote to its own pid file."""

    def __init__(self, name: str, argv: list, port: int, logdir: str):
        self.name, self.argv, self.port = name, argv, port
        self.pidfile = os.path.join(logdir, f"{name}.pid")
        self.proc = None

    def start(self):
        if not port_free(self.port):
            raise RuntimeError(f"{self.name}: port {self.port} is already taken")
        self.proc = subprocess.Popen(self.argv + [self.pidfile], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        for _ in range(100):
            if os.path.exists(self.pidfile) and not port_free(self.port):
                break
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name}: exited with {self.proc.returncode} before listening")
            time.sleep(0.05)
        with open(self.pidfile) as f:
            pid = int(f.read())
        if pid != self.proc.pid:
            raise RuntimeError(f"{self.name}: pid file does not match the process started")
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
        if str(pid) not in r.stdout.split():
            raise RuntimeError(f"{self.name}: port {self.port} is not held by pid {pid}")

    def stop(self):
        try:
            with open(self.pidfile) as f:
                pid = int(f.read())
        except (OSError, ValueError):
            return
        if self.proc and pid == self.proc.pid and self.proc.poll() is None:
            os.kill(pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.kill(pid, signal.SIGKILL)


def jsonl(path) -> list:
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def count_lines(path) -> int:
    return len(jsonl(path))


# ------------------------------------------------------------------------------------------------ hashing
def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entries(root: str):
    """(relpath, kind, mode, size, mtime_ns, content) for every entry under root; symlinks not followed."""
    if not os.path.lexists(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                yield rel, "link", st.st_mode, 0, st.st_mtime_ns, "link:" + os.readlink(p)
            elif stat.S_ISDIR(st.st_mode):
                yield rel, "dir", st.st_mode, 0, st.st_mtime_ns, ""
            elif stat.S_ISREG(st.st_mode):
                yield rel, "file", st.st_mode, st.st_size, st.st_mtime_ns, _sha(p)
            else:
                yield rel, "other", st.st_mode, 0, st.st_mtime_ns, ""


def tree_hash(root: str) -> str:
    """Strict hash (path, kind, mode, size, mtime, content): equal = nothing at all changed."""
    if not os.path.lexists(root):
        return "absent"
    h = hashlib.sha256()
    for e in entries(root):
        h.update(json.dumps(e).encode() + b"\n")
    return h.hexdigest()


def content_map(root: str) -> dict:
    """{relpath: sha256 or link:target} for files and symlinks (directories left out)."""
    return {rel: c for rel, kind, _m, _s, _t, c in entries(root) if kind in ("file", "link")}


# ------------------------------------------------------------------------------------------------ hermes
def hermes_cmd(home: str) -> list:
    if os.environ.get("EVAL_HERMES_CMD"):
        return os.environ["EVAL_HERMES_CMD"].split()
    vpy = sorted(glob.glob(os.path.join(home, "installs", "*", "environments", "*", "venv", "bin", "python")))
    src = os.environ.get("HERMES_SRC", os.path.expanduser("~/.hermes/hermes-agent"))
    if vpy and os.path.isfile(os.path.join(src, "hermes")):
        return [vpy[0], "-B", os.path.join(src, "hermes")]  # -B: no bytecode written into the Hermes install
    return [os.path.expanduser("~/.local/bin/hermes")]


def run_logged(argv, cwd, env, logbase, timeout) -> dict:
    t0 = time.monotonic()
    timed_out = False
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = None
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    wall = time.monotonic() - t0
    with open(logbase + ".out.txt", "w") as f:
        f.write(f"$ {' '.join(argv)}\n(cwd {cwd}) exit {rc} in {wall:.2f}s{' TIMEOUT' if timed_out else ''}\n\n{out}")
    with open(logbase + ".err.txt", "w") as f:
        f.write(err)
    return {"rc": rc, "stdout": out, "stderr": err, "wall_s": round(wall, 3), "timed_out": timed_out,
            "log": logbase}


def data_root(home: str) -> str:
    return os.path.join(home, "plugin-data", names.PLUGIN_NAME)


def rehearsal_ids(home: str) -> set:
    d = data_root(home)
    return {x for x in os.listdir(d) if x.startswith(names.RID_PREFIX)} if os.path.isdir(d) else set()


# ------------------------------------------------------------------------------------------------ scoring
def leaf_rows(rows: list) -> list:
    """Effects a person ticks: every row except command groups (a group's file changes are rows of their own)."""
    return [r for r in rows if not (r.get("class") == "command" and r.get("children"))]


def _tpath(r):
    return (r.get("target") or {}).get("path")


def _contains(hay, needle) -> bool:
    if needle is None:
        return True
    return needle in json.dumps(hay, ensure_ascii=False) if not isinstance(hay, str) else needle in hay


def row_matches(exp: dict, r: dict, parent_task: str | None) -> bool:
    if exp.get("class") and r.get("class") != exp["class"]:
        return False
    if exp.get("tool") and r.get("tool") != exp["tool"]:
        return False
    if "path" in exp and _tpath(r) != exp["path"]:
        return False
    if "path_glob" in exp and not fnmatch.fnmatch(_tpath(r) or "", exp["path_glob"]):
        return False
    if exp.get("change") and r.get("change") != exp["change"]:
        return False
    if "from_path" in exp and r.get("from_path") != exp["from_path"]:
        return False
    if exp.get("kind") and (r.get("target") or {}).get("kind") != exp["kind"]:
        return False
    if exp.get("url_contains") and exp["url_contains"] not in ((r.get("target") or {}).get("url") or ""):
        return False
    if exp.get("method") and ((r.get("target") or {}).get("method") or "").upper() != exp["method"]:
        return False
    for k, v in (exp.get("args_contain") or {}).items():
        if not _contains((r.get("args") or {}).get(k), v):
            return False
    if exp.get("child") is not None and parent_task is not None:
        is_child = (r.get("source") or {}).get("task_id") not in (None, parent_task)
        if is_child != exp["child"]:
            return False
    return True


def _covers(exp: dict, r: dict) -> bool:
    """A command captured without a preview covers an expected effect its command/code text names."""
    if r.get("class") != "command" or r.get("previewable", True):
        return False
    text = json.dumps(r.get("args") or {}, ensure_ascii=False)
    key = exp.get("cover_key") or exp.get("url_contains") or exp.get("path")
    return bool(key) and key in text


def bridged(r) -> list:
    """[(underlying tool, arguments)] of a row recorded under the tool_call bridge name, else []."""
    if r.get("tool") != "tool_call":
        return []
    a = r.get("args") or {}
    calls = a.get("calls") if isinstance(a.get("calls"), list) else [a]
    return [(c.get("name"), c.get("arguments") or {}) for c in calls if isinstance(c, dict) and c.get("name")]


def parent_task_id(rows: list) -> str | None:
    """task_id of the earliest captured call (the top-level session's; a child's calls carry their own)."""
    best = None
    for r in rows:
        s = r.get("source") or {}
        if s.get("task_id") and (best is None or (s.get("call_seq") or 1e9) < best[0]):
            best = (s.get("call_seq") or 1e9, s["task_id"])
    return best[1] if best else None


def score(expected: list, rows: list, parent_task: str | None) -> dict:
    """Match ground truth to ledger rows. Returns captured / covered / missed / extra and the pairing."""
    leaves = leaf_rows(rows)
    used, covering, used_bridge = set(), set(), set()
    matches, missed = [], []
    for i, exp in enumerate(expected):
        hit = next((r for r in leaves if r["id"] not in used and row_matches(exp, r, parent_task)), None)
        how = "exact"
        if hit is None and exp.get("change") == "renamed":
            # a move may be recorded as a delete plus a create
            d = next((r for r in leaves if r["id"] not in used and r.get("class") == "file"
                      and r.get("change") == "deleted" and _tpath(r) == exp.get("from_path")), None)
            c = next((r for r in leaves if r["id"] not in used and r.get("class") == "file"
                      and r.get("change") == "created" and _tpath(r) == exp.get("path")), None)
            if d and c:
                used.update((d["id"], c["id"]))
                matches.append({"expect": i, "row_ids": [d["id"], c["id"]], "how": "delete+create"})
                continue
        if hit is None and exp.get("tool"):
            # a tool_call bridge call that reached the ledger still wrapped (tool "tool_call"), single
            # ({name, arguments}) or batch ({calls: [...]}) form; one batch row may hold several effects
            b = next(((r, k) for r in leaves for k, (n, a) in enumerate(bridged(r))
                      if (r["id"], k) not in used_bridge and r["id"] not in used and n == exp["tool"]
                      and all(_contains(a.get(key) if isinstance(a, dict) else a, v)
                              for key, v in (exp.get("args_contain") or {}).items())), None)
            if b is not None:
                used_bridge.add((b[0]["id"], b[1]))
                covering.add(b[0]["id"])
                matches.append({"expect": i, "row_ids": [b[0]["id"]], "how": "bridge-wrapped"})
                continue
        if hit is None:
            cov = next((r for r in leaves if _covers(exp, r)), None)
            if cov is not None:
                covering.add(cov["id"])
                matches.append({"expect": i, "row_ids": [cov["id"]], "how": "covered_unpreviewed"})
                continue
        if hit is None:
            if not exp.get("optional"):
                missed.append({"expect": i, "what": describe(exp)})
            continue
        used.add(hit["id"])
        matches.append({"expect": i, "row_ids": [hit["id"]], "how": how})
    extra = [{"id": r["id"], "class": r.get("class"), "tool": r.get("tool"), "summary": r.get("summary")}
             for r in leaves if r["id"] not in used and r["id"] not in covering]
    required = [e for e in expected if not e.get("optional")]
    req_idx = {i for i, e in enumerate(expected) if not e.get("optional")}
    captured = sum(1 for m in matches if m["expect"] in req_idx and m["how"] in ("exact", "delete+create",
                                                                              "bridge-wrapped"))
    covered = sum(1 for m in matches if m["expect"] in req_idx and m["how"] == "covered_unpreviewed")
    return {"expected": len(required), "optional_expected": len(expected) - len(required),
            "captured": captured, "covered_unpreviewed": covered, "missed": missed, "extra": extra,
            "matches": matches, "rows": len(rows), "leaf_rows": len(leaves),
            "classes": _class_counts(rows)}


def _class_counts(rows):
    out = {}
    for r in rows:
        out[r.get("class")] = out.get(r.get("class"), 0) + 1
    return out


def describe(exp: dict) -> str:
    keys = ("class", "tool", "change", "path", "from_path", "url_contains", "args_contain", "child")
    return " ".join(f"{k}={exp[k]}" for k in keys if k in exp and exp[k] is not None)


# ------------------------------------------------------------------------------------------------ files vs ledger
def expected_after(before: dict, rows: list, applied_ids: set) -> dict:
    """Content map the workspace should hold after applying exactly these file rows, in ledger order."""
    m = dict(before)
    for r in rows:
        if r.get("class") != "file" or r["id"] not in applied_ids:
            continue
        t = r.get("target") or {}
        if not t.get("in_workspace", True):
            continue
        p, ch, after = t.get("path"), r.get("change"), r.get("after") or {}
        if ch == "deleted":
            m.pop(p, None)
        elif ch == "renamed":
            m.pop(r.get("from_path"), None)
            if after.get("exists"):
                m[p] = after.get("sha256")
        elif ch in ("created", "modified"):
            if after.get("kind") == "dir":
                continue
            if after.get("exists"):
                m[p] = after.get("sha256")
            else:
                m.pop(p, None)
    return m


def map_diff(want: dict, got: dict) -> list:
    out = []
    for p in sorted(set(want) | set(got)):
        if want.get(p) != got.get(p):
            out.append({"path": p, "want": (want.get(p) or "absent")[:16], "got": (got.get(p) or "absent")[:16]})
    return out
