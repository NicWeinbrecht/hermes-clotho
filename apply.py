"""Apply: replay exactly the ticked effects, in ledger order, with no model calls.

1. Selection: the default ticks (or --only / --except). A command's file changes follow the command.
2. Dependencies: every "effect B used effect A's would-be output" edge, recorded in the ledger or
   guessed here from paths, file contents and folder-reading commands (``dependencies``). If a ticked
   effect certainly needs an unticked one that is not already in place, Apply stops before ANY
   change, names both, and exits with EXIT_DEPENDENCY; ``force`` goes on anyway. A guessed edge
   never stops Apply: it is listed in the receipt as a guess and Apply carries on.
3. Drift check for EVERY selected effect before ANY change: each touched path must still be in its
   pre-rehearsal state (or already in exactly the state this apply would leave, which makes a
   repeated apply a no-op). Anything else stops the apply and names the files.
4. Replay: file effects write the rehearsed after-state content (byte for byte, from the blob
   store); outbound and Hermes-state effects call the real tool implementation with the stored
   arguments (other plugins' hooks and Hermes' own approval rules still apply); captured commands
   re-run for real only when named.
5. Journal: ``applied.json`` is rewritten after every effect, and an outbound effect is marked
   "started" before it is sent. A later apply never sends an effect again that was applied, that
   was cut off mid-send, or whose failure means it may have reached the other side; those are
   "held" until named with --only.
Nothing here builds a model client. The receipt's model-call count comes from this plugin's own
llm_execution middleware, which sees every model call Hermes makes in this process.
"""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import json
import os
import re
import shlex
import time

from . import capture, names, snapshot, state
from .ledger import load as load_ledger

EXIT_OK, EXIT_FAILED, EXIT_BAD_ID, EXIT_REFUSED, EXIT_DRIFT, EXIT_DEPENDENCY = 0, 1, 2, 3, 4, 5

# curl / wget exit codes that mean the request never left this machine (so a retry cannot send twice)
_CURL_NOT_SENT = {1, 2, 3, 5, 6, 7, 26, 27, 35, 37, 43, 45, 48, 49, 51, 53, 54, 58, 59, 60, 77, 83}
_WGET_NOT_SENT = {2, 3, 5}
_DONE = ("applied", "already applied", "already applied (not sent again)")


class Busy(RuntimeError):
    """Another apply of the same rehearsal holds its lock."""


@contextlib.contextmanager
def _apply_lock(rdir):
    fd = os.open(os.path.join(rdir, "apply.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another apply of this rehearsal is running; wait for it to finish") from None
        yield
    finally:
        os.close(fd)


class Drift(Exception):
    def __init__(self, items):
        super().__init__("; ".join(i["path"] + ": " + i["why"] for i in items))
        self.items = items


class UnsafePath(RuntimeError):
    """A folder on the path to an effect's target is a symlink now: the change would land elsewhere."""


def _redirect(path):
    """Where the folder holding ``path`` really is, when a symlink on the way sends it elsewhere
    (None when the folder is where its name says). The rehearsal recorded real paths, so any link
    found here appeared later and would carry the write, delete or move out of its place."""
    if not path:
        return None
    parent = os.path.normpath(os.path.dirname(os.path.abspath(path)))
    real = os.path.realpath(parent)
    return None if real == parent else real


def _check_parent(path):
    real = _redirect(path)
    if real is not None:
        raise UnsafePath(f"{os.path.dirname(path)} is now a symlink to {real}; refusing to change {path} through it")


def _leaf(row) -> bool:
    return row["class"] not in ("blocked",) and not (row["class"] == "command" and row.get("children"))


def select(rows, only=None, except_=None):
    by_id = {r["id"]: r for r in rows}
    skipped = {}
    if only:
        bad = [i for i in only if i not in by_id]
        if bad:
            raise ValueError(f"no effect numbered {', '.join(map(str, bad))}")
        sel = set(only)
    else:
        sel = {r["id"] for r in rows if r.get("default_selected") and r["apply"].get("appliable")}
    ex = set(except_ or [])
    bad = [i for i in ex if i not in by_id]
    if bad:
        raise ValueError(f"no effect numbered {', '.join(map(str, bad))}")
    # a command group selects / excludes its file changes
    for r in rows:
        if r.get("children"):
            if r["id"] in sel and r["id"] not in ex:
                for c in r["children"]:
                    if c not in ex:
                        sel.add(c)
            if r["id"] in ex:
                sel.discard(r["id"])
                for c in r["children"]:
                    sel.discard(c)
    sel -= ex
    # git internals are applied as one unit with their command, never partially
    for r in rows:
        if r.get("vcs_internal") and r.get("parent_id"):
            if r["parent_id"] in sel:
                sel.add(r["id"])
            else:
                sel.discard(r["id"])
    for i in sorted(sel):
        r = by_id[i]
        if not r["apply"].get("appliable"):
            skipped[i] = r["apply"].get("reason") or "not appliable"
    sel -= set(skipped)
    # a group is "selected" when any child is
    for r in rows:
        if r.get("children"):
            if any(c in sel for c in r["children"]):
                sel.add(r["id"])
            else:
                sel.discard(r["id"])
    return sel, skipped


# ---------------------------------------------------------------- dependencies
_TOKEN = re.compile(r"[^\s\"'`<>()\[\]{},;|=]+")
FOLDER_READERS = {"ls", "find", "tree", "du", "tar", "zip", "7z", "rsync", "fd", "gtar", "bsdtar", "ditto"}
_RECURSIVE = {"grep": ("-r", "-R", "--recursive"), "rg": ("",), "cp": ("-r", "-R", "-a"),
              "scp": ("-r",), "wc": ("",), "cat": ("",), "head": ("",)}
_GIT_FOLDER = {"add", "commit", "archive", "stash", "status", "diff"}
_TEXT_LIMIT = 256 * 1024


def _path(r):
    return (r.get("target") or {}).get("path")


def _cwd(r, workspace=None):
    t = r.get("target") or {}
    return t.get("cwd") or (r.get("args") or {}).get("workdir") or workspace


def _file_text(rdir, r):
    """The would-be content of a created or modified file, as text (None for binary or no blob)."""
    if not rdir or not r.get("blob") or r.get("change") not in ("created", "modified"):
        return None
    try:
        with open(os.path.join(rdir, r["blob"]), "rb") as f:
            raw = f.read(_TEXT_LIMIT)
    except OSError:
        return None
    if b"\0" in raw:
        return None
    return raw.decode("utf-8", "replace")


def _carried_text(r, rdir):
    """Text an effect carries out or leaves behind, where an earlier effect's path may show up."""
    c = r["class"]
    p = r.get("preview") or {}
    if c == "file":
        return _file_text(rdir, r)
    if c == "http":
        body = (p.get("request") or {}).get("body") or (r.get("target") or {}).get("body")
        return body if isinstance(body, str) else None
    if c in ("command", "blocked"):
        return None  # a command's own words are matched in the ledger's record (rule 2)
    return p.get("text") if isinstance(p.get("text"), str) and p.get("text") else None  # messages, any new class


def _segments(command):
    for seg in re.split(r"\|\||&&|[|;&\n]", command or ""):
        try:
            argv = shlex.split(seg)
        except ValueError:
            argv = seg.split()
        if argv:
            yield argv


def _folder_reads(command):
    """(reason, [path patterns]) for each part of a shell command that reads a folder's contents."""
    out = []
    for argv in _segments(command):
        prog = os.path.basename(argv[0])
        args, flags, skip = [], [], False
        for a in argv[1:]:
            if skip:
                skip = False
                continue
            if a in (">", ">>", "<", "2>", "2>>", "&>", "1>"):
                skip = True  # the next word is a redirect target, not something read
            elif a.startswith("-"):
                flags.append(a)
            elif not any(ch in a for ch in "<>"):
                args.append(a)
        globs = [a for a in args if any(ch in a for ch in "*?[")]
        if prog in FOLDER_READERS or (prog == "git" and args[:1] and args[0] in _GIT_FOLDER):
            out.append((f"`{prog}` reads the folder", [a for a in args if not any(ch in a for ch in "*?[")]
                        or ["."]))
        elif prog in _RECURSIVE and globs:
            out.append((f"`{prog}` reads every file matching {globs[0]}", globs))
        elif prog in _RECURSIVE and any(f in _RECURSIVE[prog] for f in flags):
            out.append((f"`{prog} {flags[0]}` reads the folder", args or ["."]))
        elif globs:
            out.append((f"{globs[0]} expands to the folder's files", globs))
    return out


def _under(rel, pattern):
    pattern = pattern.rstrip("/")
    if pattern in (".", "./", ""):
        return True
    pattern = pattern[2:] if pattern.startswith("./") else pattern
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(rel, pattern)
    return rel == pattern or rel.startswith(pattern + "/")


def dependencies(rows, rdir=None, workspace=None) -> list:
    """Every edge "effect B used effect A's would-be output", A always a file effect before B:
    ``{"from": A, "to": B, "sure": bool, "why": str}``. ``sure`` edges: the same file, or B's own
    command or arguments name A's path. Everything else is a guess (B's content names A's file, B
    reads the folder that holds it, or the agent read the file before B), and says so."""
    by_id = {r["id"]: r for r in rows}
    files = [r for r in rows if r["class"] == "file" and not r.get("vcs_internal") and _path(r)]
    edges = {}

    def related(a, b):
        return (a["id"] >= b["id"] or a["id"] == b.get("parent_id")
                or (a.get("parent_id") is not None and a.get("parent_id") == b.get("parent_id")))

    def add(a, b, sure, why):
        k = (a["id"], b["id"])
        if k not in edges or (sure and not edges[k]["sure"]):
            edges[k] = {"from": a["id"], "to": b["id"], "sure": sure, "why": why}

    # 1. same path (including the source of a move)
    last = {}
    for r in rows:
        if r["class"] != "file" or r.get("vcs_internal"):
            continue
        for p in filter(None, (r.get("from_path"), _path(r))):
            if p in last and not related(by_id[last[p]], r):
                add(by_id[last[p]], r, True, f"changes {p} after it")
        for p in filter(None, (r.get("from_path"), _path(r))):
            last[p] = r["id"]
    # 2. the ledger's own record
    for r in rows:
        for d in r.get("depends_on") or []:
            a = by_id.get(d)
            if a is None or a["class"] != "file" or (a["id"], r["id"]) in edges:
                continue
            p = _path(a)
            words = set(_TOKEN.findall(json.dumps(r.get("args") or {}, ensure_ascii=False)))
            words |= {w.lstrip("@") for w in words}
            if p in words or os.path.join(_cwd(r, workspace) or "", p) in words:
                add(a, r, True, f"its {'command' if 'command' in (r.get('args') or {}) else 'call'} names {p}")
            elif p == _path(r):
                add(a, r, True, f"changes {p} after it")
            elif os.path.basename(p) in json.dumps(r.get("args") or {}, ensure_ascii=False):
                add(a, r, False, f"its call mentions {os.path.basename(p)}")
            else:
                add(a, r, False, f"the agent read {p} before this")
    # 3. content that names an earlier file effect's path
    index = {}
    for a in files:
        p = _path(a)
        for key in {p, os.path.basename(p)}:
            if len(key) >= 3:
                index.setdefault(key, []).append(a["id"])
    for r in rows:
        text = _carried_text(r, rdir)
        if not text:
            continue
        seen = set()
        for w in _TOKEN.findall(text):
            for key in (w, w.rstrip(".:"), w.lstrip("@./")):
                for aid in index.get(key, ()):
                    if aid in seen:
                        continue
                    a = by_id[aid]
                    if related(a, r) or _path(a) == _path(r):
                        continue
                    seen.add(aid)
                    add(a, r, False, f"its content names {_path(a)}")
    # 4. commands that read a whole folder (ls, find, tar, globs ...)
    for r in rows:
        if r["class"] not in ("command", "http") or r.get("parent_id"):
            continue
        cmd = (r.get("target") or {}).get("command") or (r.get("args") or {}).get("command")
        reads = _folder_reads(cmd)
        if not reads:
            continue
        cwd = _cwd(r, workspace)
        takers = [by_id[c] for c in r.get("children") or [] if c in by_id and not by_id[c].get("vcs_internal")] or [r]
        for a in files:
            if a["id"] >= r["id"] or a.get("parent_id") == r["id"]:
                continue
            live = (a.get("target") or {}).get("abs_path") or ""
            if not cwd or not live.startswith(cwd.rstrip("/") + "/"):
                continue
            rel = os.path.relpath(live, cwd)
            for why, pats in reads:
                if any(_under(rel, p) for p in pats):
                    for t in takers:
                        add(a, t, False, f"{why} that holds {_path(a)}")
                    break
    return sorted(edges.values(), key=lambda e: (e["to"], e["from"]))


def _in_place(r, prev) -> bool:
    """Is file effect r already applied (journal says so, or every path it changes is in its after-state)?"""
    if prev.get(str(r["id"]), {}).get("result") == "applied":
        return True
    if r["class"] != "file":
        return False
    try:
        for _key, live, after in _paths_of(r):
            if not live or not snapshot.same_state(snapshot.fingerprint(live), after):
                return False
        return True
    except Exception:
        return False


def dependency_conflicts(rows, sel, edges, prev=None) -> list:
    """Ticked effects that need an unticked one which is not already in place (sure and guessed;
    only the sure ones stop an apply)."""
    by_id = {r["id"]: r for r in rows}
    prev = prev or {}
    out = []
    for e in edges:
        b, a = by_id[e["to"]], by_id[e["from"]]
        if e["to"] not in sel or e["from"] in sel or a["class"] == "blocked":
            continue
        if _in_place(a, prev):
            continue
        out.append({"id": b["id"], "summary": b["summary"], "needs": a["id"], "needs_summary": a["summary"],
                    "sure": e["sure"], "why": e["why"]})
    return out


def dependency_warnings(rows, sel, edges=None, prev=None):
    """The conflicts grouped by the unticked effect (the shape older receipts carried)."""
    edges = dependencies(rows) if edges is None else edges
    by_needed = {}
    for c in dependency_conflicts(rows, sel, edges, prev):
        by_needed.setdefault(c["needs"], []).append(c)
    warns = []
    for need, cs in sorted(by_needed.items()):
        ids = sorted({c["id"] for c in cs})
        guess = all(not c["sure"] for c in cs)
        warns.append({"id": need, "dependents": ids, "guess": guess,
                      "message": f"#{need} ({cs[0]['needs_summary']}) is unticked, but ticked "
                                 f"{', '.join('#' + str(d) for d in ids)} "
                                 f"{'may use' if guess else 'uses'} its output"})
    return warns


# ---------------------------------------------------------------- drift
def _paths_of(r):
    """(key, live_path, after_state) for every path a file row changes."""
    t = r.get("target") or {}
    live = t.get("abs_path") or t.get("path")
    out = []
    if r["change"] == "renamed":
        ws_root = live[: len(live) - len(t["path"])] if live.endswith(t["path"]) else None
        src_live = os.path.join(ws_root, r["from_path"]) if ws_root else None
        out.append((r["from_path"], src_live, {"exists": False}))
        out.append((t["path"], live, r.get("after") or {"exists": False}))
    elif r["change"] == "deleted":
        out.append((t["path"], live, {"exists": False}))
    else:
        out.append((t["path"], live, r.get("after") or {"exists": False}))
    return out


def drift_check(rows, sel):
    """Returns the ids of selected file effects already in place. Raises Drift before any change."""
    first_original = {}
    for r in rows:
        if r["class"] != "file":
            continue
        t = r["target"]
        if r.get("original") is not None and t["path"] not in first_original:
            first_original[t["path"]] = r["original"]
        if r["change"] == "renamed" and r.get("original_from") is not None and r["from_path"] not in first_original:
            first_original[r["from_path"]] = r["original_from"]
    final = {}
    live_of = {}
    for r in rows:
        if r["id"] in sel and r["class"] == "file":
            for key, live, after in _paths_of(r):
                final[key] = (r["id"], after)
                live_of[key] = live
    drift, done_paths = [], set()
    for key, (rid, after) in final.items():
        live = live_of[key]
        real = _redirect(live)
        if real is not None:
            drift.append({"path": key, "why": f"a folder on its path is now a symlink to {real}",
                          "symlinked_parent": real, "expected_exists": None, "found_exists": None})
            continue
        cur = snapshot.fingerprint(live) if live else {"exists": False}
        orig = first_original.get(key, {"exists": False})
        if snapshot.same_state(cur, orig):
            continue
        if snapshot.same_state(cur, after):
            done_paths.add(key)
            continue
        drift.append({"path": key, "why": "changed since the rehearsal",
                      "expected_sha256": orig.get("sha256") if orig.get("exists") else None,
                      "found_sha256": cur.get("sha256") if cur.get("exists") else None,
                      "expected_exists": bool(orig.get("exists")), "found_exists": bool(cur.get("exists"))})
    if drift:
        raise Drift(drift)
    already = set()
    for r in rows:
        if r["id"] in sel and r["class"] == "file":
            if all(k in done_paths for k, _, _ in _paths_of(r)):
                already.add(r["id"])
    return already


# ---------------------------------------------------------------- replay
def _write_blob(rdir, r, live):
    blob = os.path.join(rdir, r["blob"])
    _check_parent(live)
    os.makedirs(os.path.dirname(live), exist_ok=True)
    _check_parent(live)  # makedirs may have walked into a link made in the meantime
    tmp = live + f".{names.PLUGIN_NAME}-apply-tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    snapshot.clonefile(blob, tmp)
    mode = (r.get("after") or {}).get("mode")
    if mode and not os.path.islink(tmp):
        os.chmod(tmp, int(mode, 8))
    os.replace(tmp, live)


def _is_dir_row(r) -> bool:
    return (r.get("target") or {}).get("kind") == "dir" or (
        not r.get("blob") and (r.get("after") or {}).get("kind") == "dir")


def _make_dir(r, live):
    """A created folder: there is no blob to copy, only the folder (with its rehearsed mode)."""
    _check_parent(live)
    if os.path.lexists(live) and not (os.path.isdir(live) and not os.path.islink(live)):
        raise RuntimeError(f"{live} exists and is not a folder")
    os.makedirs(live, exist_ok=True)
    _check_parent(live)  # makedirs may have walked into a link made in the meantime
    mode = (r.get("after") or {}).get("mode")
    if mode:
        os.chmod(live, int(mode, 8))


def _apply_file(rdir, r):
    t = r["target"]
    live = t.get("abs_path") or t["path"]
    ch = r["change"]
    if ch in ("created", "modified") and _is_dir_row(r):
        _make_dir(r, live)
    elif ch in ("created", "modified"):
        _write_blob(rdir, r, live)
    elif ch == "deleted":
        _check_parent(live)
        if os.path.isdir(live) and not os.path.islink(live):
            os.rmdir(live)
        elif os.path.lexists(live):
            os.unlink(live)
    elif ch == "renamed":
        ws_root = live[: len(live) - len(t["path"])]
        src = os.path.join(ws_root, r["from_path"])
        _check_parent(src)
        _check_parent(live)
        os.makedirs(os.path.dirname(live), exist_ok=True)
        _check_parent(live)
        if (r.get("before") or {}).get("sha256") == (r.get("after") or {}).get("sha256") and os.path.lexists(src):
            os.replace(src, live)
        elif _is_dir_row(r):
            _make_dir(r, live)
            _check_parent(src)
            if os.path.isdir(src) and not os.path.islink(src) and not os.listdir(src):
                os.rmdir(src)  # only an empty folder; a folder with content moves through its files' rows
        else:
            _write_blob(rdir, r, live)
            _check_parent(src)
            if os.path.lexists(src):
                os.unlink(src)
    elif ch == "mode":
        _check_parent(live)
        mode = r.get("preview", {}).get("after_mode") or (r.get("after") or {}).get("mode")
        os.chmod(live, int(mode, 8))
    after = snapshot.fingerprint(live)
    if r.get("after") and not snapshot.same_state(after, r["after"]) and ch != "mode":
        raise RuntimeError(f"{t['path']}: content after apply does not match the rehearsal")
    return {"ok": True}


_tools_ready = False


def _real_tool(name, args, task_id):
    global _tools_ready
    import model_tools
    if not _tools_ready:
        model_tools.discover_builtin_tools()
        _tools_ready = True
    return model_tools.handle_function_call(name, args, task_id=task_id)


def _ok_result(res):
    """(ok, detail, exit_code or None)."""
    try:
        d = json.loads(res) if isinstance(res, str) else res
    except (TypeError, ValueError):
        return True, str(res)[:300], None
    if isinstance(d, dict):
        if d.get("error"):
            return False, str(d.get("error"))[:300], None
        if "exit_code" in d and d["exit_code"] not in (0, None):
            return False, f"exit code {d['exit_code']}: " + str(d.get("output"))[:200], d["exit_code"]
        if d.get("status") == "error":
            return False, str(d.get("output"))[:300], None
    return True, str(res)[:300], None


def _may_have_sent(r, exit_code, tool_error):
    """After a failed replay: could the effect have reached the other side anyway?"""
    if r["class"] == "http" and exit_code is not None:
        prog = os.path.basename(((r.get("target") or {}).get("command") or r["args"].get("command") or "x").split()[0])
        if prog == "curl":
            return exit_code not in _CURL_NOT_SENT
        if prog == "wget":
            return exit_code not in _WGET_NOT_SENT
        return True
    if r["apply"].get("method") == "tool_call" and tool_error:
        return False  # the tool itself answered with an error: it did not send
    return True


def _held_reason(entry):
    if entry.get("result") == "started":
        return "the last apply was cut off while sending it; its outcome is unknown"
    if entry.get("result") == "failed" and entry.get("sent") == "maybe":
        return f"the last attempt failed after it may have been sent ({entry.get('detail', '')[:80]})"
    return None


def _save_journal(path, prev):
    with open(path + ".tmp", "w") as f:
        json.dump(prev, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)


def _replay(rdir, meta, rid, r):
    """Run one non-file effect for real. Returns (ok, detail, exit_code, tool_error)."""
    i = r["id"]
    tid = f"{names.PLUGIN_NAME}-apply-{rid}-{i}"
    if r["apply"]["method"] == "tool_call":
        ok, info, code = _ok_result(_real_tool(r["tool"], r["args"], tid))
        return ok, info, code, not ok
    if r["apply"]["method"] == "command":
        t = r.get("target") or {}
        if r["tool"] == "execute_code":
            ok, info, code = _ok_result(_real_tool("execute_code", {"code": r["args"].get("code", "")}, tid))
        else:
            cmd = t.get("command") or r["args"].get("command")
            ok, info, code = _ok_result(_real_tool("terminal", {"command": cmd,
                                                                "workdir": t.get("cwd") or meta.get("workspace")}, tid))
        return ok, info, code, False
    return None, "nothing to apply", None, False


def apply(rid: str, only=None, except_=None, dry_run=False, force=False, out=None) -> dict:
    """Apply the selection; one apply per rehearsal at a time (two at once could both send)."""
    rdir = state.rehearsal_dir(rid)
    with _apply_lock(rdir):
        return _apply(rid, rdir, only, except_, dry_run, force)


def _apply(rid, rdir, only, except_, dry_run, force) -> dict:
    t0 = time.monotonic()
    llm0 = capture.counters()["llm_calls"]
    with open(os.path.join(rdir, "meta.json")) as f:
        meta = json.load(f)
    if meta.get("status") == "running":
        raise RuntimeError(f"rehearsal {rid} is still running; end it first")
    rows = load_ledger(os.path.join(rdir, "ledger.jsonl"))
    sel, skipped_reasons = select(rows, only, except_)
    applied_path = os.path.join(rdir, "applied.json")
    prev = {}
    if os.path.exists(applied_path):
        with open(applied_path) as f:
            prev = json.load(f)
    edges = dependencies(rows, rdir, meta.get("workspace"))
    found = dependency_conflicts(rows, sel, edges, prev)
    conflicts = [c for c in found if c["sure"]]        # these stop the apply
    guesses = [c for c in found if not c["sure"]]      # these are reported and the apply goes on
    receipt = {"schema": names.RECEIPT_SCHEMA, "rehearsal_id": rid, "workspace": meta.get("workspace"),
               "selected": sorted(sel), "warnings": dependency_warnings(rows, sel, edges, prev),
               "conflicts": conflicts, "guesses": guesses, "forced": bool(force and conflicts), "results": [],
               "applied_ids": [], "failed_ids": [], "not_attempted": [], "held_ids": [],
               "started": state.now_iso()}

    def stop(status, **kw):
        receipt.update(status=status, applied=0, model_calls=capture.counters()["llm_calls"] - llm0,
                       seconds=round(time.monotonic() - t0, 3), **kw)
        if not dry_run:
            _save_receipt(rdir, receipt)
        return receipt

    if conflicts and not force:
        return stop("dependency")
    try:
        already = drift_check(rows, sel)
    except Drift as d:
        return stop("drift", drift=d.items)
    if dry_run:
        return stop("dry-run")
    leaves = [r for r in rows if r["id"] in sel and _leaf(r)]
    applied = failed = 0
    status = "ok"
    named = set(only or [])
    for n, r in enumerate(leaves):
        i = r["id"]
        key = str(i)
        res = {"id": i, "class": r["class"], "summary": r["summary"]}
        entry = prev.get(key, {})
        if r["class"] == "file":
            if i in already:
                res.update(result="already applied")
            else:
                try:
                    _apply_file(rdir, r)
                    res.update(result="applied")
                    applied += 1
                except Exception as exc:
                    res.update(result="failed", detail=f"{type(exc).__name__}: {exc}")
        elif entry.get("result") == "applied":
            res.update(result="already applied (not sent again)")
        elif _held_reason(entry) and i not in named:
            res.update(result="held", detail=f"{_held_reason(entry)}; name it with --only {i} to send it anyway")
            receipt["held_ids"].append(i)
        else:
            prev[key] = {"result": "started", "at": state.now_iso()}
            _save_journal(applied_path, prev)  # a kill from here on leaves "started": never resent unasked
            try:
                ok, info, code, tool_error = _replay(rdir, meta, rid, r)
            except Exception as exc:
                ok, info, code, tool_error = False, f"{type(exc).__name__}: {exc}", None, False
            if ok is None:
                res.update(result="skipped", detail=info)
                prev.pop(key, None)
            elif ok:
                res.update(result="applied", detail=info)
                applied += 1
            else:
                maybe = _may_have_sent(r, code, tool_error)
                res.update(result="failed", detail=info, sent="maybe" if maybe else "no")
        receipt["results"].append(res)
        if res["result"] in _DONE:
            prev[key] = {"result": "applied", "at": state.now_iso()}
            if res["result"] == "applied":
                receipt["applied_ids"].append(i)
        elif res["result"] == "failed":
            prev[key] = {"result": "failed", "at": state.now_iso(), "detail": res.get("detail", ""),
                         "sent": res.get("sent", "no")}
        _save_journal(applied_path, prev)
        if res["result"] == "failed":
            failed += 1
            receipt["failed_ids"].append(i)
            status = "failed"
            receipt["not_attempted"] = [x["id"] for x in leaves[n + 1:]
                                        if x["id"] not in already
                                        and prev.get(str(x["id"]), {}).get("result") != "applied"]
            break
    for r in rows:  # a command group counts as applied when all its changes are
        if r.get("children") and r["id"] in sel and all(prev.get(str(c), {}).get("result") == "applied"
                                                         for c in r["children"] if c in sel):
            prev[str(r["id"])] = {"result": "applied", "at": state.now_iso()}
    _save_journal(applied_path, prev)
    if status == "ok" and receipt["held_ids"]:
        status = "incomplete"
    total = sum(1 for r in rows if _leaf(r))
    n_already = sum(1 for x in receipt["results"] if x["result"].startswith("already"))
    receipt.update(status=status, applied=applied, failed=failed, total=total,
                   skipped=total - applied - failed - n_already - len(receipt["not_attempted"]), already=n_already,
                   not_appliable=skipped_reasons,
                   model_calls=capture.counters()["llm_calls"] - llm0, seconds=round(time.monotonic() - t0, 3),
                   ended=state.now_iso())
    _save_receipt(rdir, receipt)
    return receipt


def exit_code(receipt) -> int:
    return {"ok": EXIT_OK, "dry-run": EXIT_OK, "drift": EXIT_DRIFT,
            "dependency": EXIT_DEPENDENCY}.get(receipt.get("status"), EXIT_FAILED)


def _save_receipt(rdir, receipt):
    d = os.path.join(rdir, "receipts")
    os.makedirs(d, exist_ok=True)
    n = len([x for x in os.listdir(d) if x.endswith(".json")]) + 1
    receipt["receipt_no"] = n
    with open(os.path.join(d, f"{n:03d}.json"), "w") as f:
        json.dump(receipt, f, indent=1, default=str)
