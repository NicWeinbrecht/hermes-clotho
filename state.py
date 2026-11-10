"""Rehearsal state. Nothing global except the registry of live rehearsals, everything keyed by id.

Folder of one rehearsal (``<HERMES_HOME>/plugin-data/clotho/<id>/``):
    meta.json       task, model, workspace, times, counts, model calls, cost (META_SCHEMA)
    ledger.jsonl    one effect per line (LEDGER_SCHEMA, see docs/ledger.schema.json)
    base/           APFS clone of the workspace at the start (frozen pre-state)
    work/           APFS clone where file tools and shell commands act (would-be state)
    outside/        copy-on-write overlay for file-tool writes outside the workspace
    snaps/<n>/      pre-state of shell command n (exact per-command diffs)
    home-overlay/   HOME for sandboxed commands: a mirror of the real home (writes there are OUTSIDE
                    WORKSPACE effects); file tools never use it (its folders link to the real home)
    blobs/<sha256>  after-state content of every file effect (what Apply writes)
    tmp/            private TMPDIR for sandboxed commands
    receipts/       one JSON receipt per apply
Several rehearsals of the same workspace can run at once, in one process or in several: each has its
own folder and clones, and a folder is claimed with an exclusive mkdir, so two rehearsals never share one.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import threading
import time

from . import names, snapshot


def data_root() -> str:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return str(plugin_data_dir(names.PLUGIN_NAME))
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        p = os.path.join(home, "plugin-data", names.PLUGIN_NAME)
        os.makedirs(p, exist_ok=True)
        return p


TEST_RID_ENV = names.env("TEST_RID")  # test seam: the first id this process proposes (forces a collision)
_forced_used = [False]


def new_id(allow_forced: bool = True) -> str:
    """A proposed id. Unique only once ``Rehearsal.create`` has claimed its folder (exclusive mkdir)."""
    forced = os.environ.get(TEST_RID_ENV)
    if allow_forced and forced and not _forced_used[0]:
        _forced_used[0] = True
        return forced
    return names.RID_PREFIX + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def rehearsal_dir(rid: str) -> str:
    return os.path.join(data_root(), rid)


def resolve_id(rid_or_prefix: str | None) -> str | None:
    """Full id from an id, a unique prefix, or "last"/None (most recent)."""
    root = data_root()
    ids = sorted(d for d in os.listdir(root) if d.startswith(names.RID_PREFIX)
                 and os.path.exists(os.path.join(root, d, "meta.json")))
    if not ids:
        return None
    if not rid_or_prefix or rid_or_prefix in ("last", "latest"):
        return ids[-1]
    if rid_or_prefix in ids:
        return rid_or_prefix
    hits = [i for i in ids if i.startswith(rid_or_prefix) or i.endswith(rid_or_prefix)]
    return hits[0] if len(hits) == 1 else None


class Refused(Exception):
    """A rehearsal could not be armed safely; the message says why."""


class SharedExclusive:
    """Many file-tool calls at once (shared), or one shell command alone (exclusive).

    Hermes runs the calls of one model turn on parallel workers and makes each worker wait, inside the real
    dispatch, until the calls before it have started (its start-order gate). A lock held across the real
    call would make an earlier worker wait for a later one that is itself waiting for the earlier one: the
    turn stalls until the gate gives up (120 s). So file-tool calls only share this gate (a waiting command
    never blocks a new file-tool call), and each call locks only the files it touches."""

    def __init__(self):
        self._cond = threading.Condition()
        self._shared = 0
        self._owner = None
        self._depth = 0

    def shared(self):
        return _Hold(self, False)

    def exclusive(self):
        return _Hold(self, True)

    def _acquire(self, exclusive: bool):
        me = threading.get_ident()
        with self._cond:
            if exclusive:
                while (self._owner not in (None, me)) or (self._owner is None and self._shared):
                    self._cond.wait()
                self._owner = me
                self._depth += 1
            else:
                while self._owner not in (None, me):
                    self._cond.wait()
                self._shared += 1

    def _release(self, exclusive: bool):
        with self._cond:
            if exclusive:
                self._depth -= 1
                if not self._depth:
                    self._owner = None
            else:
                self._shared -= 1
            self._cond.notify_all()


class _Hold:
    def __init__(self, gate, exclusive):
        self.gate, self.exclusive = gate, exclusive

    def __enter__(self):
        self.gate._acquire(self.exclusive)
        return self

    def __exit__(self, *exc):
        self.gate._release(self.exclusive)
        return False


class PathLocks:
    """One lock per clone/overlay path. ``hold(paths, timeout)`` takes them in a fixed order; it yields
    False when one stays busy for ``timeout`` seconds (two calls of one turn aiming at the same file)."""

    def __init__(self):
        self._locks: dict = {}
        self._guard = threading.Lock()

    def _lock(self, p):
        with self._guard:
            return self._locks.setdefault(p, threading.Lock())

    def hold(self, paths, timeout: float = 10.0):
        return _PathHold(self, sorted(set(paths)), timeout)


class _PathHold:
    def __init__(self, owner, paths, timeout):
        self.owner, self.paths, self.timeout, self.held = owner, paths, timeout, []

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        for p in self.paths:
            lk = self.owner._lock(p)
            if not lk.acquire(timeout=max(0.0, deadline - time.monotonic())):
                self.__exit__()
                return False
            self.held.append(lk)
        return True

    def __exit__(self, *exc):
        while self.held:
            self.held.pop().release()
        return False


class Rehearsal:
    def __init__(self, rid: str, workspace: str, task: str = "", mode: str = "run", model=None, provider=None,
                 session_key=None, fork_id=None, timeline=None):
        self.workspace = os.path.realpath(workspace)
        self.task = task
        self.mode = mode
        self.model, self.provider = model, provider
        self.session_key = session_key
        # optional labels for one rehearsal among several of the same task (set by a caller, or inherited
        # from the environment so a parent process can label each child process it starts)
        self.fork_id = fork_id if fork_id is not None else (os.environ.get(names.env("FORK_ID")) or None)
        self.timeline = timeline if timeline is not None else (os.environ.get(names.env("TIMELINE")) or None)
        self._set_id(rid)
        self.lock = threading.RLock()     # short bookkeeping only; never held across a real tool call
        self.id_lock = threading.Lock()   # never held while a tool runs (hooks run on other threads)
        self.fs_gate = SharedExclusive()  # file-tool calls share it, a shell command holds it alone
        self.path_locks = PathLocks()
        self.cmd_rows: dict = {}          # shell command number -> its ledger row id (late sandbox reports)
        self.next_effect_id = 1
        self.call_seq = 0
        self.cmd_seq = 0
        self.shell_cwd = self.work
        self.closed = False
        self.model_calls = 0
        self.reads: list = []          # (call_seq, kind, detail) for dependency tracking
        self.tool_calls = 0
        self.ran_for_real = 0
        self.observed_commands = 0
        self.blocked = 0
        self.meta: dict = {}

    def _set_id(self, rid: str) -> None:
        self.id = rid
        self.dir = rehearsal_dir(rid)
        self.base = os.path.join(self.dir, "base")
        self.work = os.path.join(self.dir, "work")
        self.outside = os.path.join(self.dir, "outside")
        self.snaps = os.path.join(self.dir, "snaps")
        self.blobs = os.path.join(self.dir, "blobs")
        self.tmp = os.path.join(self.dir, "tmp")
        self.home_overlay = os.path.join(self.dir, "home-overlay")  # HOME mirror for sandboxed commands
        self.ledger_path = os.path.join(self.dir, "ledger.jsonl")
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.shell_cwd = self.work

    def _claim_dir(self, attempts: int = 64) -> None:
        """Create this rehearsal's folder exclusively; on a collision take a fresh id and try again."""
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        for _ in range(attempts):
            try:
                os.mkdir(self.dir)
                return
            except FileExistsError:
                self._set_id(new_id(allow_forced=False))
        raise Refused(f"could not claim a fresh rehearsal folder in {os.path.dirname(self.dir)}")

    # ---------------------------------------------------------------- lifecycle
    def create(self) -> None:
        root = os.path.realpath(data_root())
        ws = self.workspace
        if not os.path.isdir(ws):
            raise Refused(f"workspace {ws} is not a directory")
        if root == ws or root.startswith(ws + os.sep):
            raise Refused(f"the workspace {ws} contains the Hermes home ({root}); "
                          "run from a project folder or pass --workspace")
        if ws in ("/", os.path.realpath(os.path.expanduser("~"))):
            raise Refused(f"refusing to rehearse the whole {ws}; run from a project folder or pass --workspace")
        self._claim_dir()
        for d in (self.outside, self.snaps, self.blobs, self.tmp):
            os.makedirs(d, exist_ok=True)
        t = time.monotonic()
        m1 = snapshot.clonefile(ws, self.base)
        m2 = snapshot.clonefile(ws, self.work)
        clone_s = time.monotonic() - t
        self.meta = {
            "schema": names.META_SCHEMA, "id": self.id, "plugin": names.PLUGIN_NAME, "version": names.VERSION,
            "task": self.task, "mode": self.mode, "model": self.model, "provider": self.provider,
            "workspace": ws, "started": now_iso(), "ended": None, "status": "running",
            "clone": {"method": m1 if m1 == m2 else f"{m1}/{m2}", "seconds": round(clone_s, 3),
                      "same_volume": os.stat(ws).st_dev == os.stat(self.dir).st_dev},
            "fork_id": self.fork_id, "timeline": self.timeline, "session_key": self.session_key, "pid": os.getpid(),
        }
        self.write_meta()
        open(self.ledger_path, "a").close()

    def write_meta(self, **updates) -> None:
        with self.lock:
            self.meta.update(updates)
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.meta, f, indent=1, default=str)
            os.replace(tmp, self.meta_path)

    # ---------------------------------------------------------------- paths
    def to_clone(self, abs_path: str):
        """(clone_path, rel, in_workspace). Paths outside the workspace go to the outside overlay."""
        ap = os.path.normpath(abs_path)
        ws = self.workspace
        cand = [ap]
        try:
            parent_real = os.path.realpath(os.path.dirname(ap))
            cand.append(os.path.join(parent_real, os.path.basename(ap)))
        except OSError:
            pass
        for p in cand:
            if p == ws:
                return self.work, ".", True
            if p.startswith(ws + os.sep):
                rel = os.path.relpath(p, ws)
                return os.path.join(self.work, rel), rel, True
            if p == self.work or p.startswith(self.work + os.sep):
                rel = os.path.relpath(p, self.work)
                return p, rel, True
        return os.path.join(self.outside, ap.lstrip(os.sep)), ap, False

    # Symlinks. Hermes' writer follows a final symlink (atomic_write) and the OS follows linked folders, and
    # a clone keeps absolute links as links: a lexical mapping alone could aim a rehearsed write at a real
    # file. So file-tool targets are resolved first, the way the OS would resolve them in the would-be state
    # (folders of the workspace are read from the clone, other folders from the overlay or the live disk),
    # then mapped, then checked to stay inside the clone or the overlay.
    def physical(self, live: str) -> str:
        """Where the would-be state of a live path is kept: the clone, the overlay, or the live disk."""
        ws = self.workspace
        if live == ws:
            return self.work
        if live.startswith(ws + os.sep):
            return os.path.join(self.work, os.path.relpath(live, ws))
        ov = os.path.join(self.outside, live.lstrip(os.sep))
        return ov if os.path.lexists(ov) else live

    def resolve_live(self, path: str, follow_last: bool = True, max_links: int = 40):
        """(live path with no symlink left in it, followed_a_link). ``follow_last=False`` keeps a link in
        the last component (delete and move act on the entry itself)."""
        p = os.path.normpath(path)
        if p == self.work or p.startswith(self.work + os.sep):
            p = self.workspace + p[len(self.work):]
        todo = [c for c in p.split(os.sep) if c]
        cur, links, followed = os.sep, 0, False
        while todo:
            c = todo.pop(0)
            if c == ".":
                continue
            if c == "..":
                cur = os.path.dirname(cur)
                continue
            nxt = os.path.join(cur, c)
            if not todo and not follow_last:
                cur = nxt
                break
            phys = self.physical(nxt)
            if os.path.islink(phys):
                links += 1
                if links > max_links:
                    raise OSError(errno.ELOOP, "too many levels of symbolic links", path)
                target = os.readlink(phys)
                followed = True
                if target.startswith(os.sep):
                    cur = os.sep
                todo = [x for x in target.split(os.sep) if x] + todo
                continue
            cur = nxt
        return cur, followed

    def clone_path(self, live: str):
        """(clone_path, rel, in_workspace) for a RESOLVED live path (strictly lexical)."""
        ws = self.workspace
        if live == ws:
            return self.work, ".", True
        if live.startswith(ws + os.sep):
            rel = os.path.relpath(live, ws)
            return os.path.join(self.work, rel), rel, True
        return os.path.join(self.outside, live.lstrip(os.sep)), live, False

    def contained(self, clone_path: str, entry: bool = False) -> bool:
        """True when the real path of ``clone_path`` (its parent for an entry) stays in the clone or overlay."""
        p = os.path.dirname(clone_path) if entry else clone_path
        real = os.path.realpath(p)
        for root in (self.work, self.outside):
            rr = os.path.realpath(root)
            if real == rr or real.startswith(rr + os.sep):
                return True
        return False

    def to_live(self, rel: str, in_workspace: bool) -> str:
        return os.path.join(self.workspace, rel) if in_workspace else rel

    def shell_cwd_live(self) -> str:
        c = self.shell_cwd
        if c == self.work:
            return self.workspace
        if c.startswith(self.work + os.sep):
            return os.path.join(self.workspace, os.path.relpath(c, self.work))
        return c

    def swap_out(self, text):
        """Replace clone/overlay paths in a result with the paths the agent knows."""
        if not isinstance(text, str):
            return text
        text = text.replace(self.work, self.workspace)
        text = text.replace(self.outside + os.sep, os.sep)
        return text

    def swap_in(self, text: str) -> str:
        """Point absolute workspace paths in a command at the clone."""
        if not isinstance(text, str):
            return text
        import re
        return re.sub(re.escape(self.workspace) + r"(?=$|[/\s\"'`;:|&)<>])", lambda m: self.work, text)

    def store_blob(self, path: str):
        """Keep the after-state content of a file effect; returns (sha256, relative blob path)."""
        fp = snapshot.fingerprint(path)
        if not fp.get("exists") or fp.get("kind") == "dir":
            return fp, None
        sha = fp["sha256"].replace("link:", "link-")
        dst = os.path.join(self.blobs, sha)
        if not os.path.lexists(dst):
            snapshot.clonefile(path, dst)
        return fp, os.path.join("blobs", sha)


# ---------------------------------------------------------------- live rehearsal registry
_lock = threading.RLock()
_process_rehearsal: Rehearsal | None = None      # run mode: the whole process is one rehearsal
_session_rehearsals: dict[str, Rehearsal] = {}   # session mode: keyed by session id and task id


def arm_process(r: Rehearsal) -> None:
    global _process_rehearsal
    with _lock:
        _process_rehearsal = r


def process_rehearsal() -> Rehearsal | None:
    return _process_rehearsal


def arm_session(keys, r: Rehearsal) -> None:
    with _lock:
        for k in keys:
            if k:
                _session_rehearsals[k] = r


def disarm_session(r: Rehearsal) -> None:
    with _lock:
        for k in [k for k, v in _session_rehearsals.items() if v is r]:
            _session_rehearsals.pop(k, None)


def session_rehearsal(*keys) -> Rehearsal | None:
    with _lock:
        for k in keys:
            if k and k in _session_rehearsals:
                return _session_rehearsals[k]
    return None


def current(kw: dict) -> Rehearsal | None:
    """The rehearsal that owns this call, if any."""
    r = _process_rehearsal
    if r is not None:
        return r
    if _session_rehearsals:
        return session_rehearsal(kw.get("session_id"), kw.get("task_id"))
    return None


def by_ledger(path: str) -> Rehearsal | None:
    """The live rehearsal that writes this ledger, if any."""
    with _lock:
        for r in [_process_rehearsal, *_session_rehearsals.values()]:
            if r is not None and r.ledger_path == path:
                return r
    return None


def any_armed() -> bool:
    return _process_rehearsal is not None or bool(_session_rehearsals)
