"""Capture: the tool_execution middleware, the pre_tool_call backstop and the llm_execution counter.

While a rehearsal is armed:
  * read-only tools run for real, with workspace paths pointed at the work clone;
  * write_file / patch run for real against the work clone (or the outside overlay) and become
    file effects with a diff;
  * terminal / execute_code run inside the work clone under the macOS sandbox; their file changes
    become file effects; a command that needs the network, writes outside the workspace or starts a
    background process becomes one captured effect instead ("can't preview");
  * everything else (messages, email, MCP and plugin tools, cron, memory, unknown tools) is
    captured, never run, and the agent gets an honest simulated result.
Fail closed: any error inside this module blocks the call (the real tool does not run), and the
pre_tool_call backstop blocks any effectful call that reaches Hermes' real dispatch unstamped.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sys
import threading
import time
import traceback

from . import classify, names, sandbox, snapshot, state
from .ledger import append as ledger_append

_tls = threading.local()
# Calls our middleware hands to Hermes' real dispatch. pre_tool_call hooks run on their own worker
# thread, so the stamp is keyed by tool call id (or the exact args), never by thread.
_stamps: set = set()
_stamps_lock = threading.Lock()


def _stamp_key(tool, kw_or_id, args):
    tcid = kw_or_id if isinstance(kw_or_id, str) else None
    if tcid:
        return (tool, "id", tcid)
    try:
        return (tool, "args", json.dumps(args, sort_keys=True, default=str))
    except Exception:
        return (tool, "args", repr(args))


def _call_stamped(tool, kw, args, next_call):
    tcid = kw.get("tool_call_id") or ""
    keys = {_stamp_key(tool, tcid, args), _stamp_key(tool, "", args)}
    if kw.get("_bridge"):  # Hermes dispatches the wrapper: its hooks may see the bridge's name and arguments
        wrapped = {"name": tool, "arguments": args}
        keys |= {_stamp_key(BRIDGE, tcid, wrapped), _stamp_key(BRIDGE, "", wrapped)}
    with _stamps_lock:
        _stamps.update(keys)
    try:
        return next_call(args)
    finally:
        with _stamps_lock:
            _stamps.difference_update(keys)


def _is_stamped(tool, kw) -> bool:
    keys = {_stamp_key(tool, kw.get("tool_call_id") or "", kw.get("args")), _stamp_key(tool, "", kw.get("args"))}
    with _stamps_lock:
        return bool(keys & _stamps)
_counters = {"llm_calls": 0, "tool_calls": 0}
_CTX = None

ID_KEYS = ("session_id", "task_id", "tool_call_id", "turn_id", "api_request_id")
BRIDGE = "tool_call"  # Hermes' Tool Search bridge: {name, arguments} or {calls: [...]}
REVIEW_BADGE = "BACKGROUND REVIEW"
FAULT_ENV = names.env("TEST_FAULT")  # test seam: middleware | classify | escape


def _fault(where: str) -> None:
    if os.environ.get(FAULT_ENV) == where:
        raise RuntimeError(f"injected fault ({where})")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _captured_msg(text: str) -> str:
    return (f"{names.RESULT_TAG} captured: {text} - not executed. This is a rehearsal: the effect is "
            "recorded for review and happens only if the user applies it. Any reply is unknown during the "
            "rehearsal. Continue the task as if it succeeded.")


# ---------------------------------------------------------------- rows
def _row(r: state.Rehearsal, cls: str, tool: str, args: dict, kw: dict, **fields) -> dict:
    with r.id_lock:
        eid = r.next_effect_id
        r.next_effect_id += 1
    source = {"call_seq": kw.get("_seq") or getattr(_tls, "seq", 0), **{k: kw.get(k) for k in ID_KEYS}}
    if kw.get("_bridge"):
        source["bridge"] = kw["_bridge"]
    if kw.get("_origin"):
        source["origin"] = kw["_origin"]
    row = {
        "schema": names.LEDGER_SCHEMA, "rehearsal_id": r.id, "id": eid, "class": cls, "tool": tool,
        "args": args,
        "source": source,
        "parent_id": None, "children": [], "summary": "", "target": None, "change": None,
        "irreversible": False, "previewable": True, "badges": [], "default_selected": True,
        "before": None, "original": None, "original_from": None, "after": None, "blob": None, "from_path": None,
        "vcs_internal": False, "preview": {},
        "apply": {"method": "none", "appliable": False, "reason": None},
        "depends_on": [], "dependents": [], "simulated_result": None, "ts": state.now_iso(),
    }
    row.update(fields)
    if kw.get("_origin") == "background_review":  # D37: Hermes' own review fork, not the task
        row["badges"] = list(row.get("badges") or []) + [REVIEW_BADGE]
        row["summary"] = "(background review) " + (row.get("summary") or "")
    return row


def _trace(r, **rec) -> None:
    """calls.jsonl: every tool call the rehearsal saw, its decision and outcome (evidence of coverage)."""
    try:
        rec = {"ts": round(time.time(), 3), "thread": threading.current_thread().name, **rec}
        with open(os.path.join(r.dir, "calls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _write(r: state.Rehearsal, row: dict) -> dict:
    ledger_append(r.ledger_path, row)
    return row


def _blocked(r, tool, args, kw, why, tb=None):
    try:
        r.blocked += 1
        _write(r, _row(r, "blocked", tool, args if isinstance(args, dict) else {"_raw": str(args)}, kw,
                       summary=f"blocked {tool}: {why}", previewable=False, default_selected=False,
                       badges=["BLOCKED"], preview={"error": why, "traceback": tb},
                       apply={"method": "none", "appliable": False, "reason": "blocked during the rehearsal"}))
    except Exception:
        pass


# ---------------------------------------------------------------- the middleware
def tool_execution(**kw):
    """tool_execution middleware. Not armed: pass straight through (normal behaviour)."""
    next_call = kw.get("next_call")
    args = kw.get("args")
    r = state.current(kw)
    if r is None:
        return next_call(args)
    _fault("escape")  # test seam: an exception OUTSIDE the catch-all; Hermes falls through, backstop must block
    tool = kw.get("tool_name") or "?"
    try:
        _fault("middleware")
        with r.lock:
            r.call_seq += 1
            r.tool_calls += 1
            seq = r.call_seq
        _tls.seq = seq
        kw = {**kw, "_seq": seq}
        origin = _origin()
        if origin:
            kw["_origin"] = origin
        _counters["tool_calls"] += 1
        a = dict(args or {}) if isinstance(args, dict) else {}
        if r.closed:
            _blocked(r, tool, a, kw, "the rehearsal had already ended (late call, e.g. a background job)")
            return _j({"error": f"{names.RESULT_TAG} the rehearsal has ended; {tool} was NOT run."})
        if tool != BRIDGE and _came_through_bridge(tool, a, kw.get("tool_call_id")):
            kw["_bridge"] = BRIDGE  # Hermes already unwrapped it; next_call takes the real tool's arguments
        if tool == BRIDGE:
            u = _unwrap_bridge(a)
            if u is not None:
                kind, uname, uargs = u
                if kind == "error":  # Hermes' own dispatch refuses it the same way; nothing runs
                    _trace(r, where="middleware", seq=seq, tool=tool, decision="bridge-error", error=uname,
                           tool_call_id=kw.get("tool_call_id"))
                    return _j({"error": uname})
                if kind == "batch":
                    return _capture_batch(r, uargs, kw, seq)
                tool, a, kw["_bridge"] = uname, uargs, BRIDGE
                next_call = _bridged(next_call, uname)
                _came_through_bridge(tool, a, kw.get("tool_call_id"))  # claim its note: unwrapped here, not by Hermes
        decision = classify.decide(tool, a, mode=r.mode)
        _trace(r, where="middleware", seq=seq, tool=tool, decision=decision, bridge=kw.get("_bridge"),
               origin=kw.get("_origin"), tool_call_id=kw.get("tool_call_id"))
        if decision == "run":
            plan = _plan_read(r, tool, a)
        elif decision == "file":
            return _file_effect(r, tool, a, kw, next_call)
        elif decision == "shell":
            return _shell_effect(r, tool, a, kw)
        else:
            return _capture_call(r, tool, a, kw, decision)
    except Exception as exc:  # FAIL CLOSED: never let Hermes fall through to the real tool
        _blocked(r, tool, args, kw, f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=6))
        return _j({"error": f"{names.RESULT_TAG} {names.PRODUCT} hit an internal error ({type(exc).__name__}: "
                            f"{exc}); {tool} was NOT run. Nothing happened."})
    # read-only path: the real read runs outside the catch-all so its own errors stay tool errors
    mapped, note = plan
    with r.lock:
        r.ran_for_real += 1
    held = getattr(_tls, "reading", None)
    _tls.reading = r
    try:
        with (usage_kept(r) if tool in _BUMPING_READS else _nothing()):
            res = _call_stamped(tool, kw, mapped, next_call)
    finally:
        _tls.reading = held
    try:
        return r.swap_out(res) if isinstance(res, str) else res
    except Exception:
        return res


# ---------------------------------------------------------------- the tool_call bridge
def _unwrap_bridge(a):
    """What a ``tool_call`` would run, resolved with Hermes' own parser: ("single", name, args),
    ("batch", None, entries), ("error", message, None), or None when the bridge cannot be read here (the
    wrapper is then captured as an unknown tool: fail closed)."""
    try:
        from tools import tool_search as ts
        name, uargs, err = ts.resolve_underlying_call(a)
        sentinel = getattr(ts, "CONNECTOR_BATCH_SENTINEL", None)
    except Exception:
        return None
    if err:
        return "error", str(err), None
    if not name:
        return None
    if sentinel is not None and name == sentinel:
        entries = (uargs or {}).get("calls") or []
        return "batch", None, [e for e in entries if isinstance(e, dict)]
    return "single", str(name), dict(uargs or {}) if isinstance(uargs, dict) else {}


def _bridged(next_call, name):
    """Hermes' dispatch still receives the bridge's own shape; only the inner arguments are ours."""
    return lambda inner: next_call({"name": name, "arguments": inner})


def _capture_batch(r, entries, kw, seq):
    """A bridged batch (connector tools only, by Hermes' rule): one captured row per entry, never run."""
    results = []
    for i, e in enumerate(entries):
        name = str(e.get("name") or "?")
        args = e.get("arguments") if isinstance(e.get("arguments"), dict) else {}
        ekw = {**kw, "_bridge": BRIDGE, "tool_call_id": (f"{kw['tool_call_id']}#{i}" if kw.get("tool_call_id") else None)}
        decision = classify.decide(name, args, mode=r.mode)
        if decision not in ("outbound", "state", "unknown"):
            decision = "outbound"  # a batch entry is never run for real
        _trace(r, where="middleware", seq=seq, tool=name, decision=decision, bridge="batch",
               tool_call_id=ekw.get("tool_call_id"))
        out = json.loads(_capture_call(r, name, args, ekw, decision))
        results.append({"index": i, "name": name, "response": out})
    return _j({"results": results})


def _origin():
    """"background_review" while Hermes' background memory/skill review fork is calling (D37)."""
    try:
        from tools.skill_provenance import get_current_write_origin
        o = get_current_write_origin()
    except Exception:
        return None
    return o if o in ("background_review", "refine_review") else None  # "assistant_tool" / "foreground": the task


# ---------------------------------------------------------------- reads that write bookkeeping
# skill_view bumps view/use counters in <home>/skills/.usage.json after a successful read
# (tools/skills_tool.py _skill_view_with_bump -> tools/skill_usage.bump_view / bump_use), and a cron job's
# skills are bumped while its prompt is built. The read runs for real; afterwards the counters it moved are
# put back under Hermes' own lock for that file (only the records that changed, to what they were), so a
# read leaves the home as it was. Nothing in Hermes is replaced. kanban_list promotes tasks whose parents
# are done (recompute_ready, a board write): it is held instead (classify.CAPTURE_READS).
_BUMPING_READS = {"skill_view"}
_usage_active = [0]          # usage_kept blocks running now (parallel reads of one turn)
_usage_active_lock = threading.Lock()


@contextlib.contextmanager
def _nothing():
    yield


@contextlib.contextmanager
def usage_kept(r):
    """Run the block; then undo any change it made to Hermes' skill usage counters."""
    try:
        import tools.skill_usage as su
        path = su._usage_file()
        lock = path.with_suffix(".json.lock")
        existed, lock_existed = path.exists(), lock.exists()
        before = su.load_usage() if existed else {}
    except Exception:
        su = None
    with _usage_active_lock:
        _usage_active[0] += 1
    try:
        yield
    finally:
        with _usage_active_lock:
            _usage_active[0] -= 1
            last = _usage_active[0] == 0
        if su is not None:
            try:
                _undo_usage(r, su, path, lock, before, existed, lock_existed or not last)
            except Exception as exc:
                _trace(r, where="bookkeeping", error=f"{type(exc).__name__}: {exc}")


def _undo_usage(r, su, path, lock, before, existed, lock_existed) -> None:
    with su._usage_file_lock():
        now = su.load_usage() if path.exists() else {}
        moved = sorted(k for k in set(now) | set(before) if now.get(k) != before.get(k))
        if moved:
            for k in moved:
                if k in before:
                    now[k] = before[k]
                else:
                    now.pop(k, None)
            if not existed and not now:
                path.unlink(missing_ok=True)
            else:
                su.save_usage(now)
            _trace(r, where="bookkeeping", held="skill_usage counters", undone=moved[:10])
    if not lock_existed:
        try:
            lock.unlink()
        except OSError:
            pass


def _abs_for(r: state.Rehearsal, path: str) -> str:
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(r.shell_cwd_live(), p)
    return os.path.normpath(p)


def _plan_read(r, tool, a):
    """Point read tools at the work clone (or the overlay) so they see the would-be state."""
    if tool in classify.READ_PATH_TOOLS:
        raw = a.get("path") or "."
        ap = _abs_for(r, raw)
        try:
            live, _via = r.resolve_live(ap)
            cp, rel, inws = r.clone_path(live)
            if tool == "read_file" and not inws and not os.path.lexists(cp) and os.path.isfile(live):
                with r.lock:  # read and later write the same overlay copy (Hermes ties writes to reads by path)
                    os.makedirs(os.path.dirname(cp), exist_ok=True)
                    snapshot.clonefile(live, cp)
        except OSError:
            cp, rel, inws = r.to_clone(ap)
        if inws or os.path.lexists(cp):
            a = {**a, "path": cp}
        r.reads.append((r.call_seq, tool, f"{rel} {a.get('pattern') or ''}"))
    return a, None


# ---------------------------------------------------------------- file tools
def _target(r, ap, entry=False):
    """Resolve a file-tool target the way the OS would in the would-be state, map it into the clone or the
    overlay and prepare the overlay. Returns (live, clone_path, rel, in_workspace, via_link) or a reason
    (str) why the target cannot be rehearsed safely."""
    try:
        live, via = r.resolve_live(ap, follow_last=not entry)
    except OSError as exc:
        return f"cannot resolve {ap} ({exc.strerror or exc})"
    cp, rel, inws = r.clone_path(live)
    if not inws and not os.path.lexists(cp):
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        if os.path.lexists(live) and (entry or os.path.isfile(live)):
            snapshot.clonefile(live, cp)
    if not r.contained(cp, entry=entry) or (not entry and os.path.islink(cp)):
        return f"{ap} resolves to {os.path.realpath(cp)}, outside the rehearsal's copy"
    return live, cp, rel, inws, via


def _original(r, live, rel, inws):
    return snapshot.fingerprint(os.path.join(r.base, rel)) if inws else snapshot.fingerprint(live)


def _file_row(r, tool, a, kw, t, before, before_blob, after, after_blob, original, res, requested=None):
    live, cp, rel, inws, via = t
    if not after.get("exists"):
        change = "deleted"
    elif not before.get("exists"):
        change = "created"
    else:
        change = "modified"
    label = rel if inws else live
    diff = snapshot.unified_paths(os.path.join(r.dir, before_blob) if before_blob else None,
                                  cp if change != "deleted" else None, label, label)
    plus, minus = snapshot.diff_stats(diff)
    badges = ([] if inws else ["OUTSIDE WORKSPACE"]) + (["VIA SYMLINK"] if via else [])
    verb = {"created": "create", "modified": "modify", "deleted": "delete"}[change]
    target = {"kind": "file", "path": rel if inws else live, "abs_path": live, "in_workspace": inws}
    if via and requested:
        target["requested_path"] = requested
    return _row(r, "file", tool, a, kw, change=change, target=target,
                summary=f"{verb} {label} (+{plus} -{minus})" + (f" (through a symlink: {requested})" if via and requested else ""),
                before=before, original=original if _first_touch(r, rel if inws else live) else None,
                after=after, blob=after_blob if change != "deleted" else None, badges=badges,
                preview={"diff": diff, "lines_added": plus, "lines_removed": minus,
                         "result": (r.swap_out(res)[:400] if isinstance(res, str) else None)},
                apply={"method": "unlink" if change == "deleted" else "write_blob", "appliable": True, "reason": None})


def _file_effect(r, tool, a, kw, next_call):
    if tool == "patch" and str(a.get("mode") or "replace") == "patch":
        return _v4a_effect(r, tool, a, kw, next_call)
    raw = a.get("path")
    if not isinstance(raw, str) or not raw:
        _blocked(r, tool, a, kw, "no path argument")
        return _j({"error": f"{names.RESULT_TAG} {tool} needs a path; nothing was written."})
    ap = _abs_for(r, raw)
    # Never hold r.lock across the real call: Hermes' start-order gate sits inside it (see state.SharedExclusive).
    with r.fs_gate.shared():
        with r.lock:
            t = _target(r, ap)
            if isinstance(t, str):
                return _cant_preview_tool(r, tool, a, kw, [t])
        live, cp, rel, inws, via = t
        with r.path_locks.hold([cp]) as ok:
            if not ok:
                return _cant_preview_tool(r, tool, a, kw, [f"another call of the same turn was still writing {raw}"])
            with r.lock:
                original = _original(r, live, rel, inws)
                before, before_blob = r.store_blob(cp)
            res = _call_stamped(tool, kw, {**a, "path": cp}, next_call)
            with r.lock:
                after, after_blob = r.store_blob(cp)
                if snapshot.same_state(before, after):
                    err = _tool_error(res)
                    # Refused by the tool itself (outside, through a link, or a guard such as "not read first"):
                    # keep the attempt visible. A patch whose old text is not found is a failed edit, not shown.
                    if err and (not inws or via or tool == "write_file" or re.search(r"\brefus", err, re.I)):
                        _write(r, _refused_row(r, tool, a, kw, [t + (raw,)], r.swap_out(err)))
                    return r.swap_out(res) if isinstance(res, str) else res
                _write(r, _file_row(r, tool, a, kw, t, before, before_blob, after, after_blob, original, res,
                                    requested=raw))
    return r.swap_out(res) if isinstance(res, str) else res


def _tool_error(res):
    """The error a file tool answered with, or None."""
    if not isinstance(res, str):
        return None
    try:
        d = json.loads(res)
    except ValueError:
        s = res.strip()
        return s[:600] if s.lower().startswith(("error", "[write_file]", "[patch]")) or "Refusing" in s[:200] else None
    if isinstance(d, dict):
        if d.get("error"):
            return str(d["error"])[:600]
        if d.get("success") is False:
            return str(d.get("message") or d.get("result") or res)[:600]
    return None


def _refused_row(r, tool, a, kw, targets, err):
    """A file-tool call the tool refused (nothing was written, in the rehearsal or for real). Not an
    effect, but an attempt a reviewer must see: the target the agent aimed at and the tool's message.
    ``targets``: (live, clone_path, rel, in_workspace, via_link, requested) or (None, None, raw, None, None, raw)."""
    r.blocked += 1
    shown = []
    for live, _cp, rel, inws, via, req in targets:
        shown.append({"path": (rel if inws else live) if live else req, "abs_path": live, "in_workspace": inws,
                      "requested_path": req, "via_symlink": bool(via)})
    first = shown[0] if shown else {"path": "?", "abs_path": None, "in_workspace": None}
    outside = any(s["in_workspace"] is False for s in shown)
    badges = ["BLOCKED"] + (["OUTSIDE WORKSPACE"] if outside else []) + \
             (["VIA SYMLINK"] if any(s["via_symlink"] for s in shown) else [])
    what = first["path"] if len(shown) == 1 else f"{first['path']} and {len(shown) - 1} more"
    target = {"kind": "file", "path": first["path"], "in_workspace": bool(first["in_workspace"])}
    if first.get("abs_path"):
        target["abs_path"] = first["abs_path"]
    if first.get("requested_path") and first["requested_path"] != first["path"]:
        target["requested_path"] = first["requested_path"]
    return _row(r, "blocked", tool, a, kw, summary=f"{tool} refused by Hermes: {what} - {_short(err, 90)}",
                target=target, previewable=False, default_selected=False, badges=badges,
                preview={"error": err, "refused_by": "tool", "targets": shown},
                apply={"method": "none", "appliable": False,
                       "reason": "the tool refused it during the rehearsal; nothing would be written"})


# V4A patches (patch mode="patch") name their targets in header lines; Hermes resolves them against the task's
# folder, not against `path`. Every header is resolved and rewritten to its place in the clone or overlay.
_V4A_MARKERS_FALLBACK = (("update", re.compile(r"\*\*\*\s*Update\s+File:\s*(.+)")),
                         ("add", re.compile(r"\*\*\*\s*Add\s+File:\s*(.+)")),
                         ("delete", re.compile(r"\*\*\*\s*Delete\s+File:\s*(.+)")),
                         ("move", re.compile(r"\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)")))


def _v4a_markers():
    """Hermes' own header patterns (the ones its parser applies), or a copy when they cannot be imported."""
    try:
        from tools.patch_parser import _OP_MARKERS
        return [(str(getattr(k, "value", k)).lower(), rx) for k, rx in _OP_MARKERS]
    except Exception:
        return list(_V4A_MARKERS_FALLBACK)


def _v4a_effect(r, tool, a, kw, next_call):
    text = a.get("patch")
    if not isinstance(text, str) or not text.strip():
        _blocked(r, tool, a, kw, "patch mode without patch content")
        return _j({"error": f"{names.RESULT_TAG} patch needs patch content; nothing was written."})
    markers = _v4a_markers()
    try:  # Hermes' own validation first (it rejects '..' in headers): the same error the live call would give
        from tools.file_tools import _collect_v4a_header_paths
        err = _collect_v4a_header_paths(text)
        if isinstance(err, str):
            with r.lock:
                _write(r, _refused_row(r, tool, a, kw, _v4a_raw_targets(r, text, markers), r.swap_out(_err_text(err))))
            return err
    except Exception:
        pass
    with r.fs_gate.shared():
        return _v4a_apply(r, tool, a, kw, next_call, text, markers)


def _err_text(err):
    try:
        d = json.loads(err)
        return str(d.get("error") or err) if isinstance(d, dict) else str(err)
    except (TypeError, ValueError):
        return str(err)


def _v4a_raw_targets(r, text, markers):
    """Header paths as written (for a refused patch's row), resolved lexically only."""
    out = []
    for line in text.split("\n"):
        core = line.rstrip("\r")
        hit = next((m for _kind, rx in markers if (m := rx.match(core))), None)
        if not hit:
            continue
        for g in range(1, (hit.lastindex or 1) + 1):
            raw = (hit.group(g) or "").strip()
            if not raw:
                continue
            ap = _abs_for(r, raw)
            _cp, rel, inws = r.to_clone(ap)
            out.append((ap, None, rel, inws, False, raw))
    return out or [(None, None, "?", None, None, "?")]


def _v4a_apply(r, tool, a, kw, next_call, text, markers):
    targets = {}  # clone path -> target tuple, in header order
    out_lines = []
    with r.lock:
        for line in text.split("\n"):
            core = line[:-1] if line.endswith("\r") else line
            hit = next(((kind, m) for kind, rx in markers if (m := rx.match(core))), None)
            if not hit:
                out_lines.append(line)
                continue
            kind, m = hit
            raws = [m.group(1).strip()] + ([m.group(2).strip()] if kind == "move" else [])
            mapped = []
            for raw in raws:
                t = _target(r, _abs_for(r, raw), entry=kind in ("delete", "move"))
                if isinstance(t, str):
                    return _cant_preview_tool(r, tool, a, kw, [t])
                targets.setdefault(t[1], t + (raw,))
                mapped.append(t[1])
            new = core[:m.start(1)] + mapped[0] + (core[m.end(1):m.start(2)] + mapped[1] if kind == "move" else "")
            out_lines.append(new + ("\r" if line.endswith("\r") else ""))
        if not targets:
            return _cant_preview_tool(r, tool, a, kw, ["the patch names no file in a form the rehearsal can map"])
        call_args = {**a, "patch": "\n".join(out_lines)}
        if isinstance(a.get("path"), str) and a["path"]:
            t = _target(r, _abs_for(r, a["path"]))
            if isinstance(t, str):
                return _cant_preview_tool(r, tool, a, kw, [t])
            call_args["path"] = t[1]
    with r.path_locks.hold(list(targets)) as ok:
        if not ok:
            with r.lock:
                return _cant_preview_tool(r, tool, a, kw, ["another call of the same turn was still writing one of its files"])
        with r.lock:
            states = {}
            for cp, t in targets.items():
                states[cp] = (_original(r, t[0], t[2], t[3]),) + r.store_blob(cp)
        res = _call_stamped(tool, kw, call_args, next_call)
        with r.lock:
            rows = []
            for cp, t in targets.items():
                original, before, before_blob = states[cp]
                after, after_blob = r.store_blob(cp)
                if snapshot.same_state(before, after):
                    continue
                rows.append(_file_row(r, tool, a, kw, t[:5], before, before_blob, after, after_blob, original, res,
                                      requested=t[5]))
            err = _tool_error(res)
            if not rows and err and any(not t[3] or t[4] for t in targets.values()):
                # the tool refused a patch aimed outside the workspace or through a link: keep the attempt visible
                rows.append(_refused_row(r, tool, a, kw, list(targets.values()), r.swap_out(err)))
            for row in rows:
                _write(r, row)
    return r.swap_out(res) if isinstance(res, str) else res


def _cant_preview_tool(r, tool, a, kw, reasons):
    """A file-tool call that cannot be rehearsed safely: captured, not run, unticked."""
    why = "; ".join(reasons)
    msg = (f"{names.RESULT_TAG} captured, not run: {why}. Nothing happened. It is recorded for review and runs only "
           "if the user applies it. Continue the task as if it succeeded.")
    _write(r, _row(r, "outbound", tool, a, kw, summary=f"{tool} - can't preview: {why}",
                   target={"kind": "tool", "name": tool, "recipients": []},
                   irreversible=True, previewable=False, default_selected=False,
                   badges=["CAN'T PREVIEW", "IRREVERSIBLE"],
                   preview={"reasons": reasons, "args": a, "text": a.get("content") or a.get("patch") or ""},
                   apply={"method": "tool_call", "appliable": True,
                          "reason": "runs for real only when you name it (--only N); its effects were not previewed"},
                   simulated_result=msg))
    return _j({"success": True, "rehearsal": True, "message": msg})


def _first_touch(r, key) -> bool:
    seen = getattr(r, "_touched", None)
    if seen is None:
        seen = r._touched = set()
    if key in seen:
        return False
    seen.add(key)
    return True


# ---------------------------------------------------------------- shell
# execute_code: the sandbox log does not always report a blocked python process (seen in testing), so code
# also becomes can't-preview when it names a network module or its error output shows the sandbox stopped it.
_CODE_NET = re.compile(r"\b(urllib3?|requests|httpx|aiohttp|http\.client|socket|smtplib|ftplib|telnetlib|"
                       r"websockets?|xmlrpc|paramiko|open_connection|create_connection)\b")
_CODE_DENIED = re.compile(r"Operation not permitted|PermissionError|Permission denied|URLError|"
                          r"ConnectionRefused|Connection refused|EPERM|EACCES|Errno 1\b|Errno 13\b|Errno 61\b")


def _code_reasons(code: str, res: dict) -> list:
    out = []
    if _CODE_NET.search(code or "") and not res.get("blocked_network"):
        out.append("may use the network (the code names a network module)")
    if res.get("exit_code") not in (0, None) and _CODE_DENIED.search(res.get("stderr") or ""):
        out.append("was stopped by the sandbox (see its error output)")
    return out


# python started from the terminal tool (python3 -c, a heredoc, a script in the workspace) gets the same static
# rule: Python run with -I or -E skips the audit hook, and then only the lossy sandbox log could see a connection.
_PY_CMD = re.compile(r"(?:^|[\s;&|(`/])python(?:[0-9.]*)?(?=$|[\s;&|)`])")
_PY_SCRIPT = re.compile(r"(?:^|\s)([\w./~-]+\.py)\b")


def _inline_code_reasons(r, command: str, cwd: str, res: dict) -> list:
    if not _PY_CMD.search(command or "") or res.get("blocked_network"):
        return []
    text = command
    for rel in _PY_SCRIPT.findall(command):
        p = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
        p = r.swap_in(os.path.expanduser(p))
        try:
            if os.path.isfile(p) and os.path.realpath(p).startswith(os.path.realpath(r.work) + os.sep):
                with open(p, encoding="utf-8", errors="replace") as f:
                    text += "\n" + f.read(262144)
        except OSError:
            pass
    if _CODE_NET.search(text):
        return ["may use the network (the Python it runs names a network module)"]
    return []


def _outside_row(r, tool, a, kw, o, parent_id):
    """A change a sandboxed command made in the home mirror: an OUTSIDE WORKSPACE effect on the real path it
    stands for (sandbox.run's ``outside_changes``). Apply writes it to the real path after its drift check."""
    live, change, kind = o["abs_path"], o["change"], o.get("kind", "file")
    ov, bp = o.get("overlay_path"), o.get("before_path")
    before = snapshot.fingerprint(bp) if bp and change != "created" else {"exists": False}
    after, blob = r.store_blob(ov) if change != "deleted" else ({"exists": False}, None)
    if change == "deleted" and bp:
        r.store_blob(bp)
    diff = None
    if kind == "file":
        diff = snapshot.unified_paths(bp if change != "created" else None, ov if change != "deleted" else None,
                                      o.get("from") or live, live)
    plus, minus = snapshot.diff_stats(diff)
    verb = {"created": "create", "modified": "modify", "deleted": "delete", "renamed": "move", "mode": "chmod"}[change]
    label = f"{o['from']} -> {live}" if change == "renamed" else live
    original = snapshot.fingerprint(live) if _first_touch(r, live) else None
    original_from = snapshot.fingerprint(o["from"]) if change == "renamed" and _first_touch(r, o["from"]) else None
    return _row(r, "file", tool, a, kw, change=change, parent_id=parent_id, original_from=original_from,
                target={"kind": kind, "path": live, "abs_path": live, "in_workspace": False},
                from_path=o.get("from"), badges=["OUTSIDE WORKSPACE"],
                summary=f"{verb} {label}" + (f" (+{plus} -{minus})" if diff else ""),
                before=before, original=original, after=after, blob=blob,
                preview={"diff": diff, "lines_added": plus, "lines_removed": minus, "home_mirror": o.get("rel"),
                         "before_mode": o.get("before_mode"), "after_mode": o.get("after_mode")},
                apply={"method": {"deleted": "unlink", "renamed": "rename", "mode": "chmod"}.get(change, "write_blob"),
                       "appliable": True, "reason": None})


def late_rows(r) -> list:
    """BLOCKED rows for sandbox reports that reached the log after their command's result was returned
    (sandbox.late_violations). Called once when the rehearsal ends, before the ledger is finalized."""
    out = []
    try:
        late = sandbox.late_violations(f"{names.PLUGIN_NAME}-{r.id}-0")
    except Exception:
        return out
    seen = getattr(r, "_late_seen", None)
    if seen is None:
        seen = r._late_seen = set()
    for v in late:
        key = (v.get("tag"), v.get("operation"), v.get("target"), v.get("pid"))
        if key in seen:
            continue
        seen.add(key)
        try:
            n = int(str(v.get("tag", "")).rsplit("-", 1)[1])
        except (ValueError, IndexError):
            n = None
        row_id, tool, display = r.cmd_rows.get(n, (None, "terminal", None))
        what = f"{v.get('operation')} {v.get('target')}".strip()
        cmd = f"command #{row_id}" if row_id else f"`{_short(display or '?')}`"
        with r.lock:
            r.blocked += 1
            row = _write(r, _row(r, "blocked", tool or "terminal", {"command": display} if display else {}, {},
                                 summary=f"blocked after {cmd} returned: {_short(what, 80)}",
                                 target={"kind": "command", "command": display, "cwd": r.workspace},
                                 previewable=False, default_selected=False, badges=["BLOCKED"],
                                 preview={"error": f"the sandbox stopped {what} after the command's result was "
                                                   "returned; its preview may be incomplete",
                                          "violation": v, "command_row": row_id},
                                 apply={"method": "none", "appliable": False,
                                        "reason": "a blocked action is never applied"}))
        out.append(row)
    return out


def _shell_effect(r, tool, a, kw):
    if tool == "execute_code":
        code = a.get("code") or ""
        if "hermes_tools" in code:
            return _cant_preview(r, tool, a, kw, ["calls Hermes tools from code"], None, None)
        n = r.cmd_seq + 1
        code_path = os.path.join(r.tmp, f"code_{n}.py")
        os.makedirs(r.tmp, exist_ok=True)
        with open(code_path, "w") as f:
            f.write(r.swap_in(code))
        command = f"{shlex.quote(sys.executable)} {shlex.quote(code_path)}"
        display = code
    else:
        command = a.get("command") or ""
        display = command
    if a.get("background"):
        return _cant_preview(r, tool, a, kw, ["runs in the background"], None, None)
    ok, why = sandbox.available()
    if not ok:
        return _cant_preview(r, tool, a, kw, [why], None, None)
    cwd = r.shell_cwd
    if a.get("workdir"):
        cp, rel, inws = r.to_clone(_abs_for(r, a["workdir"]))
        if not inws:
            return _cant_preview(r, tool, a, kw, ["runs outside the workspace"], None, None)
        cwd = cp
    if not os.path.isdir(cwd):
        cwd = r.work
    scan = sandbox.static_scan(display)
    with r.fs_gate.exclusive(), r.lock:
        r.cmd_seq += 1
        n = r.cmd_seq
        snap = os.path.join(r.snaps, str(n))
        snapshot.clonefile(r.work, snap)
        pre = snapshot.stat_manifest(r.work)
        res = sandbox.run(r.work, cwd, r.swap_in(command) if tool == "terminal" else command, r.tmp,
                          tag=f"{names.PLUGIN_NAME}-{r.id}-{n}", timeout=min(int(a.get("timeout") or 60), 180),
                          orig=r.workspace, home_overlay=r.home_overlay)  # D36: HOME is a mirror of the home
        post = snapshot.stat_manifest(r.work)
        changes = snapshot.diff_effects(snap, r.work, pre, post)
        outside = list(res.get("outside_changes") or [])
        if _reads_files(display, tool):
            r.reads.append((r.call_seq, tool, display))
        reasons = sandbox.reasons_cant_preview(res, scan)
        if tool == "execute_code":
            reasons += [x for x in _code_reasons(display, res) if x not in reasons]
        else:
            reasons += [x for x in _inline_code_reasons(r, display, cwd, res) if x not in reasons]
        if reasons:
            if changes:  # the would-be state is unknown: roll the clone back to before this command
                sandbox.move_aside(r.work, os.path.join(r.dir, "discarded"))
                snapshot.clonefile(snap, r.work)
            if res.get("home_overlay"):  # and the home mirror (sandbox.run already did when it saw the reason)
                sandbox.restore_home(res)
            return _cant_preview(r, tool, a, kw, reasons, res, changes, scan, n=n)
        if res.get("cwd_after"):
            c = os.path.realpath(res["cwd_after"])
            if c == os.path.realpath(r.work) or c.startswith(os.path.realpath(r.work) + os.sep):
                r.shell_cwd = os.path.join(r.work, os.path.relpath(c, os.path.realpath(r.work)))
                r.shell_cwd = os.path.normpath(r.shell_cwd)
        out = r.swap_out(res["stdout"] + (res["stderr"] if res["stderr"] else ""))
        verb = "run code" if tool == "execute_code" else "run"
        preview = {"stdout": r.swap_out(res["stdout"])[-4000:], "stderr": r.swap_out(res["stderr"])[-2000:],
                   "exit_code": res["exit_code"], "sandbox": _sb_summary(res), "scan": scan}
        if tool == "execute_code":
            preview["code"] = display
        if not changes and not outside:
            r.observed_commands += 1
            r.cmd_rows[n] = (None, tool, display)
            if tool == "execute_code":  # code never disappears from the ledger, even when it changed nothing
                row = _write(r, _row(r, "command", tool, a, kw, target={"kind": "command", "command": display,
                                                                        "cwd": r.to_live(os.path.relpath(cwd, r.work), True)},
                                     summary=f"{verb} `{_short(display)}` (no file changes)", default_selected=False,
                                     preview=preview,
                                     apply={"method": "none", "appliable": False,
                                            "reason": "it changed no files in the rehearsal; nothing to apply"}))
                r.cmd_rows[n] = (row["id"], tool, display)
            return _shell_result(tool, out, res["exit_code"])
        total = len(changes) + len(outside)
        cmd_row = _row(r, "command", tool, a, kw, target={"kind": "command", "command": display,
                                                          "cwd": r.to_live(os.path.relpath(cwd, r.work), True)},
                       summary=f"{verb} `{_short(display)}` ({total} file change{'s' if total != 1 else ''}"
                               + (f", {len(outside)} outside the workspace" if outside else "") + ")",
                       preview=preview,
                       apply={"method": "group", "appliable": True,
                              "reason": "applies the command's file changes listed under it"})
        r.cmd_rows[n] = (cmd_row["id"], tool, display)
        rows = [_change_row(r, tool, a, kw, c, snap, cmd_row["id"]) for c in changes]
        rows += [_outside_row(r, tool, a, kw, o, cmd_row["id"]) for o in outside]
        cmd_row["children"] = [row["id"] for row in rows]
        _write(r, cmd_row)
        for row in rows:
            _write(r, row)
        return _shell_result(tool, out, res["exit_code"])


READ_HEADS = {"cat", "head", "tail", "grep", "egrep", "rg", "wc", "less", "more", "sed", "awk", "jq", "diff",
              "cut", "sort", "uniq", "tr", "base64", "md5", "shasum", "xxd", "od", "strings", "python", "python3",
              "node", "ruby", "perl", "zip", "tar", "gzip", "cp", "rsync", "scp", "curl", "wget"}


def _reads_files(command: str, tool: str) -> bool:
    """Does this command read file contents (so later outbound effects may carry them)?"""
    if tool == "execute_code":
        return True
    for tok in (command or "").replace("|", " ").replace(";", " ").replace("&", " ").split():
        if os.path.basename(tok) in READ_HEADS:
            return True
    return False


def _short(s: str, n: int = 60) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _sb_summary(res):
    if not res:
        return None
    return {k: res.get(k) for k in ("detection", "log_reader", "blocked_network", "blocked_write", "blocked_read",
                                    "blocked_exec", "blocked_ipc", "survivors", "swept", "timed_out", "seconds",
                                    "audit_events")} | {
        "maybe_blocked": list(res.get("maybe_blocked") or []), "violations": res.get("violations", [])[:20]}


def _change_row(r, tool, a, kw, c, snap, parent_id):
    rel = c["path"]
    live_before = os.path.join(snap, c.get("from") or rel)
    work_path = os.path.join(r.work, rel)
    before = snapshot.fingerprint(live_before) if c["change"] != "created" else {"exists": False}
    after, blob = (r.store_blob(work_path) if c["change"] != "deleted" else ({"exists": False}, None))
    if c["change"] in ("deleted",):
        r.store_blob(live_before)  # keep a copy of what a delete removes
    diff = None
    if c.get("kind") == "file":
        diff = snapshot.unified_paths(live_before if c["change"] != "created" else None,
                                      work_path if c["change"] != "deleted" else None,
                                      c.get("from") or rel, rel)
    plus, minus = snapshot.diff_stats(diff)
    verb = {"created": "create", "modified": "modify", "deleted": "delete", "renamed": "move", "mode": "chmod"}[c["change"]]
    label = f"{c['from']} -> {rel}" if c["change"] == "renamed" else rel
    original = snapshot.fingerprint(os.path.join(r.base, rel)) if _first_touch(r, rel) else None
    original_from = None
    if c["change"] == "renamed" and _first_touch(r, c["from"]):
        original_from = snapshot.fingerprint(os.path.join(r.base, c["from"]))
    return _row(r, "file", tool, a, kw, change=c["change"], parent_id=parent_id, original_from=original_from,
                target={"kind": c.get("kind", "file"), "path": rel, "abs_path": os.path.join(r.workspace, rel),
                        "in_workspace": True},
                from_path=c.get("from"), vcs_internal=bool(c.get("vcs_internal")),
                summary=f"{verb} {label}" + (f" (+{plus} -{minus})" if diff else ""),
                before=before, original=original, after=after, blob=blob,
                preview={"diff": diff, "lines_added": plus, "lines_removed": minus,
                         "before_mode": c.get("before_mode"), "after_mode": c.get("after_mode")},
                apply={"method": {"deleted": "unlink", "renamed": "rename", "mode": "chmod"}.get(c["change"], "write_blob"),
                       "appliable": True, "reason": None})


def _shell_result(tool, out, code):
    if tool == "execute_code":
        return _j({"status": "success" if code == 0 else "error", "output": out, "exit_code": code})
    return _j({"output": out, "exit_code": code, "error": None})


def _cant_preview(r, tool, a, kw, reasons, res, changes, scan=None, n=None):
    display = a.get("command") if tool == "terminal" else a.get("code")
    http = sandbox.parse_http(display) if tool == "terminal" and display else None
    only_network = reasons == ["needs the network"]
    if http and only_network:
        if http.get("body_file"):  # show the would-be content the request will carry (from the clone)
            bf = os.path.join(r.shell_cwd, os.path.expanduser(r.swap_in(http["body_file"])))
            try:
                with open(bf, "rb") as f:
                    raw = f.read(65537)
                http["body"] = raw[:65536].decode("utf-8", "replace")
                http["body_note"] = f"contents of {http['body_file']} as rehearsed"
            except OSError:
                pass
        body = http.get("body")
        n = len(body.encode()) if isinstance(body, str) else 0
        what = f"would {http['method']} {http['url']}" + (f" ({n} bytes)" if n else "")
        irreversible = http["method"] not in ("GET", "HEAD", "OPTIONS")
        msg = _captured_msg(what + " (not sent; the response is unknown during a rehearsal)")
        row = _write(r, _row(r, "http", tool, a, kw, summary=f"{http['method']} {http['url']}", simulated_result=msg,
                             target={"kind": "http", **http},
                             irreversible=irreversible, badges=(["IRREVERSIBLE"] if irreversible else []) + ["NETWORK"],
                             preview={"request": http, "sandbox": _sb_summary(res),
                                      "note": "the command was stopped at the network in the sandbox; nothing was sent"},
                             apply={"method": "command", "appliable": True,
                                    "reason": "re-runs this exact command for real in the workspace"}))
        if n is not None:
            r.cmd_rows[n] = (row["id"], tool, display)
        return _shell_result(tool, msg, 0)
    why = "; ".join(reasons)
    verb = "run code" if tool == "execute_code" else "run"
    partial = []
    if changes:
        partial = [{"path": c["path"], "change": c["change"]} for c in changes][:50]
    for o in ((res or {}).get("outside_changes_discarded") or [])[:50]:
        partial.append({"path": o.get("abs_path"), "change": o.get("change"), "outside_workspace": True})
    msg = (f"{names.RESULT_TAG} captured, not run: this command {'; '.join(reasons)}, so it cannot be previewed "
           "safely. Nothing happened and its real output is unknown. It is recorded for review and runs only if the "
           "user applies it. Continue the task as if it succeeded.")
    row = _write(r, _row(r, "command", tool, a, kw, simulated_result=msg,
                         summary=f"{verb} `{_short(display or '')}` - can't preview: {why}",
                         target={"kind": "command", "command": display, "cwd": r.shell_cwd_live()},
                         irreversible=True, previewable=False, default_selected=False,
                         badges=["CAN'T PREVIEW", "IRREVERSIBLE"],
                         preview={"reasons": reasons, "stdout": r.swap_out((res or {}).get("stdout", ""))[-3000:],
                                  "stderr": r.swap_out((res or {}).get("stderr", ""))[-2000:],
                                  "exit_code": (res or {}).get("exit_code"), "sandbox": _sb_summary(res),
                                  "partial_changes_discarded": partial, "scan": scan,
                                  **({"code": display} if tool == "execute_code" else {})},
                         apply={"method": "command", "appliable": True,
                                "reason": "runs for real only when you name it (--only N); its effects were not previewed"}))
    if n is not None:
        r.cmd_rows[n] = (row["id"], tool, display)
    return _shell_result(tool, msg, 0)


# ---------------------------------------------------------------- everything else
HELD_WHY = {"kanban_list": "listing the board also promotes tasks whose parents are done, a board write"}

def _capture_call(r, tool, a, kw, decision):
    recips, text = classify.recipients_and_text(a)
    if decision == "state":
        recips = []
    held_read = tool in classify.CAPTURE_READS
    irreversible = decision in ("outbound", "unknown") and not held_read
    appliable, reason = True, None
    if tool in classify.AGENT_LOOP_TOOLS:
        appliable, reason = False, "this tool needs the agent loop; it cannot be replayed without a model in this version"
    what = f"would call {tool}" + (f" -> {', '.join(recips)}" if recips else "")
    if tool == "cronjob_manage":
        what = f"would {a.get('action') or 'change'} cron job {a.get('name') or a.get('job_id') or ''}".rstrip()
    if held_read:
        what = f"would fetch with {tool}" + (f" -> {', '.join(recips)}" if recips else "") + \
               f" (held: {HELD_WHY.get(tool, 'a fetch can trigger a webhook and is metered')})"
    badges = (["IRREVERSIBLE"] if irreversible else []) + (["UNKNOWN TOOL"] if decision == "unknown" else []) + \
             (["HELD READ"] if held_read else [])
    msg = _captured_msg(what)
    _write(r, _row(r, decision, tool, a, kw, summary=what.replace("would ", "", 1),
                   target={"kind": "tool", "name": tool, "recipients": recips},
                   irreversible=irreversible, badges=badges,
                   default_selected=decision != "unknown" and appliable and not held_read,
                   preview={"recipients": recips, "text": text, "args": a},
                   apply={"method": "tool_call", "appliable": appliable, "reason": reason},
                   simulated_result=msg))
    return _j({"success": True, "rehearsal": True, "message": msg})


# ---------------------------------------------------------------- backstop
def pre_tool_call(**kw):
    """While armed, block any effectful call that reaches Hermes' real dispatch without our stamp
    (our middleware failed, or a path that runs hooks before middleware). Never raises."""
    try:
        r = state.current(kw)
        if r is None:
            return None
        tool = kw.get("tool_name")
        if _is_stamped(tool, kw):
            return None
        if not r.closed and classify.decide(tool, {}, mode=r.mode) == "run":
            return None  # a read: it cannot escape
        _trace(r, where="backstop", tool=tool, tool_call_id=kw.get("tool_call_id"))
        _blocked(r, tool, kw.get("args") or {}, kw, "reached real dispatch without the rehearsal's stamp (backstop)")
        return {"action": "block", "message": f"{names.RESULT_TAG} {tool} was blocked during a rehearsal. "
                                              "Nothing happened."}
    except Exception:
        return {"action": "block", "message": f"{names.RESULT_TAG} blocked (backstop error). Nothing happened."}


# ---------------------------------------------------------------- llm counter / ACP refusal
def llm_execution(**kw):
    next_call = kw.get("next_call")
    request = kw.get("request")
    try:
        _counters["llm_calls"] += 1
        r = state.current(kw)
        if r is not None:
            r.model_calls += 1
            base_url = str(kw.get("base_url") or "")
            api_mode = str(kw.get("api_mode") or "")
            why = None
            if base_url.startswith("acp") or "acp" in api_mode.lower():
                why = f"provider runs tools outside Hermes ({base_url or api_mode})"
            else:
                probs = _provider_problems_cached(str(kw.get("provider") or ""))
                if probs:
                    why = probs[0]
            if why:
                r.write_meta(status="refused", refused=why)
                if r.mode == "run":
                    sys.__stderr__.write(f"{names.PRODUCT}: refusing to continue: {why}; a rehearsal cannot "
                                         "capture what it does.\n")
                    sys.__stderr__.flush()
                    os._exit(4)
    except Exception:
        pass
    resp = next_call(request)
    try:
        if state.current(kw) is not None:
            _note_bridge_calls(resp)
    except Exception:
        pass
    return resp


# ---------------------------------------------------------------- calls Hermes unwrapped from the bridge
# Hermes peels a single-form tool_call to a tool the session may reach BEFORE the tool_execution middleware
# runs (agent/tool_executor._unwrap_tool_search_call), so the middleware sees only the real tool's name. The
# model's own response still says it called tool_call: llm_execution notes every such call (by its id, and by
# the underlying name and arguments in case Hermes renamed the id), and the row gets source.bridge.
_bridge_seen: dict = {}   # tool call id -> (underlying name, arguments key)
_bridge_keys: dict = {}   # (underlying name, arguments key) -> how many are still unclaimed
_bridge_lock = threading.Lock()
_BRIDGE_MEMORY = 2000


def _g(o, k):
    return o.get(k) if isinstance(o, dict) else getattr(o, k, None)


def _tool_calls_of(resp):
    """(id, name, arguments) of every tool call in a provider response: chat completions (choices[].message
    .tool_calls), Anthropic messages (content[] tool_use) and Responses (output[] function_call)."""
    out = []
    for ch in _g(resp, "choices") or []:
        for tc in _g(_g(ch, "message") or {}, "tool_calls") or []:
            fn = _g(tc, "function") or {}
            out.append((_g(tc, "id") or _g(tc, "call_id"), _g(fn, "name"), _g(fn, "arguments")))
    for blk in _g(resp, "content") or [] if not isinstance(_g(resp, "content"), str) else []:
        if _g(blk, "type") == "tool_use":
            out.append((_g(blk, "id"), _g(blk, "name"), _g(blk, "input")))
    for it in _g(resp, "output") or [] if not isinstance(_g(resp, "output"), str) else []:
        if _g(it, "type") == "function_call":
            out.append((_g(it, "call_id") or _g(it, "id"), _g(it, "name"), _g(it, "arguments")))
    return out


def _args_key(args) -> str:
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return repr(args)


def _bridge_entries(args) -> list:
    """(name, arguments) of each call Hermes runs on its own from this tool_call: the single form, or each
    entry of a batch that names a local tool, which Hermes splits into single calls before they are run
    (agent/tool_call_batches.expand_local_tool_batches) and then unwraps like the single form. A batch of
    connector tools only stays one call and reaches the middleware as tool_call (_capture_batch)."""
    u = _unwrap_bridge(args)
    if u and u[0] == "single":
        return [(u[1], u[2])]
    try:
        from tools.tool_search_validation import normalize_tool_call_entries
        from tools.connectors import is_connector_name
        entries, err = normalize_tool_call_entries(args)
    except Exception:
        return []
    if err or len(entries) < 2 or all(is_connector_name(e.get("name")) for e in entries):
        return []
    return [(str(e.get("name")), e.get("arguments") if isinstance(e.get("arguments"), dict) else {})
            for e in entries]


def _note_bridge_calls(resp) -> None:
    for cid, name, raw in _tool_calls_of(resp):
        if name != BRIDGE:
            continue
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            continue
        for k, (uname, uargs) in enumerate(_bridge_entries(args)):
            key = (uname, _args_key(uargs))
            with _bridge_lock:
                if cid and k == 0:  # the first entry of a split batch keeps the call's id; the rest get new ones
                    _bridge_seen[str(cid).split("|", 1)[0]] = key
                _bridge_keys[key] = _bridge_keys.get(key, 0) + 1
                while len(_bridge_seen) > _BRIDGE_MEMORY:
                    _bridge_seen.pop(next(iter(_bridge_seen)))
                while len(_bridge_keys) > _BRIDGE_MEMORY:
                    _bridge_keys.pop(next(iter(_bridge_keys)))


def _came_through_bridge(tool, args, tool_call_id) -> bool:
    """Did the model call ``tool`` through tool_call (Hermes having unwrapped it)? Claims the note."""
    cid = str(tool_call_id or "").split("|", 1)[0]
    key = (tool, _args_key(args))
    with _bridge_lock:
        hit = None
        for c in (cid, re.sub(r"_d\d+$", "", cid)):
            if c and c in _bridge_seen and _bridge_seen[c][0] == tool:
                hit = _bridge_seen.pop(c)
                break
        if hit is None and _bridge_keys.get(key):
            hit = key
        if hit is None:
            return False
        if _bridge_keys.get(hit):
            _bridge_keys[hit] -= 1
            if not _bridge_keys[hit]:
                _bridge_keys.pop(hit)
        return True


# ---------------------------------------------------------------- providers (D35)
# A provider that runs tools outside Hermes' tool path cannot be rehearsed. The Claude-subscription provider
# drives the claude CLI; it is allowed only when the installed version is one whose native tools were verified
# off (every request passes --tools '' with an inert MCP server, dontAsk, one turn) and no command override can
# swap the binary. The same check runs on every route: --provider, HERMES_INFERENCE_PROVIDER, the configured
# default (all resolve to agent.provider, which llm_execution sees at the first model call).
CLAUDE_SUBSCRIPTION = {
    "claude-subscription-directsdk-experimental": {
        "versions": {"0.3.4"},
        "files": {"directsdk.py": ("'--tools', ''", "'--permission-mode', 'dontAsk'", "'--strict-mcp-config'",
                                   "'--max-turns', '1'", "'--setting-sources', ''"),
                  "inert_mcp.py": ("native tools are inert",)},
        "env_overrides": ("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND",),
    },
}
_provider_cache: dict = {}


def effective_provider(requested=None) -> str:
    """The provider Hermes will use: an explicit name, else config, else HERMES_INFERENCE_PROVIDER, else auto."""
    try:
        from hermes_cli.runtime_provider import resolve_requested_provider
        return str(resolve_requested_provider(requested) or "auto").lower()
    except Exception:
        return str(requested or os.environ.get("HERMES_INFERENCE_PROVIDER") or "auto").strip().lower()


def _plugin_dir(name: str):
    home = os.environ.get("HERMES_HOME")
    try:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    except Exception:
        pass
    root = os.path.join(home or os.path.expanduser("~/.hermes"), "plugins")
    cand = [os.path.join(root, name)]
    try:
        cand += [os.path.join(root, d) for d in sorted(os.listdir(root)) if d != name]
    except OSError:
        pass
    for d in cand:
        try:
            with open(os.path.join(d, "plugin.yaml"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r"^name:\s*['\"]?([^'\"\s]+)", text, re.M)
        if m and m.group(1) == name:
            v = re.search(r"^version:\s*['\"]?([^'\"\s]+)", text, re.M)
            return d, (v.group(1) if v else None)
    return None, None


def claude_native_tools_off(name: str):
    """(True, how it was checked) when this Claude-subscription provider's native tools are verified off."""
    spec = CLAUDE_SUBSCRIPTION.get(name)
    if spec is None:
        return False, f"'{name}' is not a Claude-subscription provider whose native tools were verified off"
    d, version = _plugin_dir(name)
    if not d:
        return False, f"the plugin providing '{name}' was not found in this Hermes home"
    if version not in spec["versions"]:
        return False, (f"'{name}' {version} is not a verified version ({', '.join(sorted(spec['versions']))}); "
                       "its native tools may be on")
    for fname, needles in spec["files"].items():
        try:
            with open(os.path.join(d, fname), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return False, f"'{name}': {fname} is missing, so the native-tools switch cannot be checked"
        missing = [x for x in needles if x not in text]
        if missing:
            return False, f"'{name}': {fname} no longer passes {', '.join(missing)} (native tools may be on)"
    for env in spec["env_overrides"]:
        if os.environ.get(env):
            return False, f"'{name}': {env} points at another claude command, so its flags cannot be checked"
    return True, f"{name} {version}: native tools off (--tools '', inert MCP server, dontAsk, one turn)"


def provider_problem(name, base_url="") -> str:
    """Why this (already resolved) provider must not drive a rehearsal; "" = allowed. cli.selfcheck calls it
    before the model starts and llm_execution at the first model call, so every route gets the same answer."""
    n = str(name or "").strip().lower()
    if "acp" in n or str(base_url or "").startswith("acp"):
        return "runs tools outside Hermes' tool path; a rehearsal cannot capture them"
    if n in CLAUDE_SUBSCRIPTION or "claude-subscription" in n or "claude-code" in n:
        ok, why = claude_native_tools_off(n)
        return "" if ok else f"may run tools outside Hermes' tool path: {why}"
    return ""


def provider_problems(provider=None) -> list:
    """Reasons a rehearsal must not use this provider (empty = allowed). ``provider`` is the --provider value
    or None; the configured default and HERMES_INFERENCE_PROVIDER are resolved the way Hermes resolves them."""
    name = effective_provider(provider)
    why = provider_problem(name)
    return [f"provider '{name}' {why}"] if why else []


def _provider_problems_cached(provider: str) -> list:
    if not provider:
        return []
    if provider not in _provider_cache:
        _provider_cache[provider] = provider_problems(provider)
    return _provider_cache[provider]


def counters() -> dict:
    return dict(_counters)
