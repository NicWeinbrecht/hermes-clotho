"""The effect report: terminal text, a compact card for chat sessions, and --json. All rendering
lives here.

Two layers:
  * ``describe`` / ``tally``: pure per-effect and per-run summaries (no colour, no width, no I/O),
    stable enough to line several rehearsals up side by side;
  * ``render_*``: terminal text built from them.

Terminal contract: at most WIDTH (58) columns per line by default; colour only when stdout is a terminal
(or FORCE_COLOR is set) and NO_COLOR is not, and then named ANSI only: bold (1), dim (2), reverse (7) and
one hue, red (31), which marks nothing but the thread you keep (the kept timeline and its "● keep", the
receipt's "0 model calls"). The terminal theme supplies the colours. The IRREVERSIBLE badge is
"✂  IRREVERSIBLE" in reverse video at normal intensity in the default colours (ESC[22;39;7m … ESC[27m),
never red and never dim, followed by the row's own attribute again; without colour it is "[IRREVERSIBLE]".
(✂ is drawn about two cells wide by the fallback font, so one extra space follows it.) Every style is
closed by its own code (22, 39, 27), never by a full reset. Unknown classes, badges and fields render
generically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata

from . import names, state
from .apply import dependencies, select
from .ledger import counts, load

WIDTH = 58
COLLAPSE_MIN = 5          # this many similar effects in a row collapse into one line
MAX_TEXT_LINES = 12

# style -> (open, close). Named ANSI only, closed by its own code so a surrounding style survives.
ANSI = {"bold": ("1", "22"), "dim": ("2", "22"), "red": ("31", "39"), "reverse": ("7", "27")}
BADGE_OPEN = "\033[22;39;7m"     # normal intensity, default colours, reverse video: Ink on Paper
BADGE_CLOSE = "\033[27m"
BADGE_TEXT = " ✂  IRREVERSIBLE "  # ✂ draws about two cells wide, so it gets one space more
BADGE_PLAIN = "[IRREVERSIBLE]"
BADGE_STYLE = {"IRREVERSIBLE": ("badge",), "CAN'T PREVIEW": ("bold",), "OUTSIDE WORKSPACE": ("bold",),
               "BLOCKED": ("bold",), "UNKNOWN TOOL": ("bold",), "NETWORK": (), "HELD READ": ("dim",),
               "VIA SYMLINK": ("bold",)}  # any other badge: dim, as given
CLASS_LABEL = {"file": "files", "command": "commands", "http": "web", "outbound": "messages",
               "state": "Hermes state", "unknown": "unknown tools", "blocked": "blocked"}
CLASS_ORDER = ["file", "command", "http", "outbound", "state", "unknown", "blocked"]


def _use_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    s = stream or sys.stdout
    return hasattr(s, "isatty") and s.isatty()


def _open(*styles) -> str:
    codes = [ANSI[s][0] for s in styles if s in ANSI]
    return "\033[" + ";".join(codes) + "m" if codes else ""


def _close(*styles) -> str:
    codes = []
    for s in reversed(styles):
        if s in ANSI and ANSI[s][1] not in codes:
            codes.append(ANSI[s][1])
    return "\033[" + ";".join(codes) + "m" if codes else ""


def _c(text, *styles, on=True):
    styles = tuple(s for s in styles if s in ANSI)
    if not on or not styles or not text:
        return text
    return _open(*styles) + text + _close(*styles)


def badge(on=True, row=()) -> str:
    """The IRREVERSIBLE badge. ``row``: the styles of the text around it, opened again after the badge
    (the badge's own codes cancel dim and red)."""
    if not on:
        return BADGE_PLAIN
    return BADGE_OPEN + BADGE_TEXT + BADGE_CLOSE + _open(*row)


def badge_width(on=True) -> int:
    return vwidth(BADGE_TEXT if on else BADGE_PLAIN)


# ---------------------------------------------------------------- width helpers
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean(s) -> str:
    """One line of untrusted text, safe to print: tabs to spaces, control characters (ESC included) shown as ?."""
    return _CTRL.sub("?", str(s).replace("\t", "  ").replace("\r", ""))


def _cw(ch) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def vwidth(s: str) -> int:
    """Columns s takes on screen (SGR codes take none)."""
    return sum(_cw(ch) for ch in _SGR_RE.sub("", s))


def fit(s: str, width: int) -> str:
    """Cut s to at most width columns, ending in … when cut."""
    if vwidth(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        if w + _cw(ch) > width - 1:
            break
        out.append(ch)
        w += _cw(ch)
    return "".join(out) + "…"


def fit_left(s: str, width: int) -> str:
    """Cut the start of s (for paths: keep the end)."""
    if vwidth(s) <= width:
        return s
    out, w = [], 0
    for ch in reversed(s):
        if w + _cw(ch) > width - 1:
            break
        out.append(ch)
        w += _cw(ch)
    return "…" + "".join(reversed(out))


def wrap(s: str, width: int) -> list:
    """Word-wrap to width columns; words longer than a line are split."""
    width = max(width, 8)
    lines, cur = [], ""
    for word in s.split(" "):
        while vwidth(word) > width:
            room = width - vwidth(cur) - (1 if cur else 0)
            if room < 4:
                lines.append(cur)
                cur, room = "", width
            head, w = [], 0
            for ch in word:
                if w + _cw(ch) > room:
                    break
                head.append(ch)
                w += _cw(ch)
            cut = max((k for k, ch in enumerate(head) if ch in "/-_.&?=,"), default=-1)
            if cut >= len(head) // 3:  # break a long path or URL after a separator
                head = head[:cut + 1]
            cur = (cur + " " if cur else "") + "".join(head)
            lines.append(cur)
            cur, word = "", word[len(head):]
        if not cur:
            cur = word
        elif vwidth(cur) + 1 + vwidth(word) <= width:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def wrap_parts(parts, width, sep=" · ") -> list:
    """Join parts with sep, breaking lines only between parts (a part longer than width is wrapped)."""
    lines, cur = [], ""
    for p in parts:
        cand = p if not cur else cur + sep + p
        if vwidth(cand) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        if vwidth(p) <= width:
            cur = p
        else:
            *full, cur = wrap(p, width)
            lines.extend(full)
    if cur:
        lines.append(cur)
    return lines


def _width(width):
    if width is None:
        return WIDTH
    if width == 0:
        return max(40, shutil.get_terminal_size((WIDTH, 24)).columns)
    return max(40, int(width))


# ---------------------------------------------------------------- data
def load_rehearsal(rid: str):
    rdir = state.rehearsal_dir(rid)
    with open(os.path.join(rdir, "meta.json")) as f:
        meta = json.load(f)
    rows = load(os.path.join(rdir, "ledger.jsonl"))
    applied_path = os.path.join(rdir, "applied.json")
    applied = {}
    if os.path.exists(applied_path):
        with open(applied_path) as f:
            applied = json.load(f)
    return meta, rows, applied


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _sha(s) -> str:
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:16]


_VERB = {"created": "create", "modified": "modify", "deleted": "delete", "renamed": "move", "mode": "chmod"}


def describe(r: dict) -> dict:
    """Pure summary of one effect: what it is, what it acts on, how to group and match it.

    ``key`` names the effect's target independently of its number, so the same effect can be found in
    another rehearsal of the same task; ``payload`` fingerprints what it would write or send, so two
    rehearsals that hit the same target with different content can be told apart."""
    cls = r.get("class") or "unknown"
    t = r.get("target") or {}
    p = r.get("preview") or {}
    tool = r.get("tool") or ""
    badges = [str(b) for b in (r.get("badges") or [])]
    d = {"id": r.get("id"), "class": cls, "tool": tool, "summary": _clean(r.get("summary") or tool or cls),
         "badges": badges, "irreversible": bool(r.get("irreversible")),
         "previewable": r.get("previewable", True) is not False, "ticked_default": bool(r.get("default_selected")),
         "appliable": bool((r.get("apply") or {}).get("appliable")), "parent_id": r.get("parent_id"),
         "is_group": bool(r.get("children")), "hidden": bool(r.get("vcs_internal"))}
    if cls == "file":
        ch = r.get("change") or "modified"
        d.update(kind={"renamed": "file move", "deleted": "delete"}.get(ch, "file change"), verb=_VERB.get(ch, ch),
                 object=t.get("path"), source=r.get("from_path"),
                 added=p.get("lines_added") or 0, removed=p.get("lines_removed") or 0,
                 key=f"file:{ch}:{r.get('from_path') or ''}:{t.get('path')}",
                 payload=(r.get("after") or {}).get("sha256") or "")
    elif cls == "http":
        req = p.get("request") or t
        d.update(kind="web request", verb=str(req.get("method") or "GET"), object=req.get("url"),
                 key=f"http:{req.get('method')}:{req.get('url')}", payload=_sha(req.get("body") or ""))
    elif cls == "command":
        cmd = t.get("command") or (r.get("args") or {}).get("command") or (r.get("args") or {}).get("code")
        d.update(kind="command", verb="run", object=cmd, key=f"command:{cmd}",
                 payload=_sha(p.get("stdout") or ""))
    else:
        recips = p.get("recipients") or t.get("recipients") or []
        kind = {"outbound": "message", "state": "Hermes state change", "unknown": "unknown tool call",
                "blocked": "blocked call"}.get(cls, cls)
        d.update(kind=kind, verb="call", object=", ".join(map(str, recips)) or tool,
                 key=f"{cls}:{tool}:{','.join(sorted(map(str, recips)))}",
                 payload=_sha(json.dumps(r.get("args") or {}, sort_keys=True, default=str)))
    d["group"] = (cls, r.get("change"), d.get("verb") if cls in ("http",) else None,
                  tool if cls not in ("file", "http", "command") else None, tuple(badges),
                  d["ticked_default"], d["appliable"], d["previewable"])
    return d


def tally(rows: list, applied: dict | None = None) -> dict:
    """Pure counts for one rehearsal: effects by class and kind, irreversible, can't preview, ticked,
    applied. An effect is something Apply acts on: a command group counts through its file changes."""
    by_class, kinds = {}, {}
    n = irr = cp = ticked = done = 0
    for r in rows:
        if r.get("vcs_internal") or (r.get("children") and r.get("class") == "command"):
            continue
        d = describe(r)
        n += 1
        by_class[d["class"]] = by_class.get(d["class"], 0) + 1
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
        irr += d["irreversible"]
        cp += not d["previewable"]
        ticked += d["ticked_default"] and d["appliable"]
        done += (applied or {}).get(str(r.get("id")), {}).get("result") == "applied"
    return {"effects": n, "by_class": by_class, "kinds": kinds, "irreversible": irr, "cant_preview": cp,
            "ticked_default": ticked, "applied": done}


def summary_line(rows, applied=None) -> str:
    c = counts(rows)
    parts = []
    files = [r for r in rows if r["class"] == "file" and not r.get("vcs_internal")]
    moves = sum(1 for r in files if r["change"] == "renamed")
    deletes = sum(1 for r in files if r["change"] == "deleted")
    other_files = len(files) - moves - deletes
    if other_files:
        parts.append(_plural(other_files, "file change"))
    if moves:
        parts.append(_plural(moves, "file move"))
    if deletes:
        parts.append(_plural(deletes, "delete"))
    if c.get("http"):
        irr = sum(1 for r in rows if r["class"] == "http" and r.get("irreversible"))
        parts.append(_plural(c["http"], "web request") + (" (irreversible)" if irr else ""))
    if c.get("outbound"):
        parts.append(_plural(c["outbound"], "message/outbound call", "messages/outbound calls") + " (irreversible)")
    if c.get("state"):
        parts.append(_plural(c["state"], "Hermes state change"))
    if c.get("unknown"):
        parts.append(_plural(c["unknown"], "unknown tool call") + " (irreversible)")
    cp = sum(1 for r in rows if r["class"] == "command" and not r.get("previewable", True))
    if cp:
        parts.append(_plural(cp, "command") + " can't be previewed")
    if c.get("blocked"):
        parts.append(_plural(c["blocked"], "blocked call"))
    for k in sorted(set(c) - set(CLASS_ORDER) - {"irreversible", "cant_preview", "deletes", "creates", "modifies",
                                                 "renames"}):
        if c[k]:
            parts.append(f"{c[k]} {k}")
    if not parts:
        parts.append("no effects")
    leaves = {str(r["id"]) for r in rows if r["class"] != "blocked" and not (r["class"] == "command" and r.get("children"))}
    done = sum(1 for k, v in (applied or {}).items() if v.get("result") == "applied" and k in leaves)
    happened = f"{done} applied" if done else "0 things happened yet"
    return " · ".join(parts + [happened])


def to_json(rid: str) -> dict:
    meta, rows, applied = load_rehearsal(rid)
    sel, _ = select(rows)
    return {"rehearsal": meta, "summary": summary_line(rows, applied), "counts": counts(rows),
            "default_selection": sorted(sel), "applied": applied, "effects": rows}


def class_parts(t: dict) -> list:
    order = CLASS_ORDER + sorted(set(t["by_class"]) - set(CLASS_ORDER))
    return [f"{CLASS_LABEL.get(k, k)} {t['by_class'][k]}" for k in order if t["by_class"].get(k)]


# ---------------------------------------------------------------- terminal report
def _badge(b, on, row=()):
    """One badge as shown; ``row`` is the style of the text around it (only the IRREVERSIBLE badge
    resets it, and opens it again after itself)."""
    style = BADGE_STYLE.get(b, ("dim",))
    if style == ("badge",):
        return badge(on, row)
    if not on:
        return b
    return _c(b, *style, on=on) + (_open(*row) if row and style else "")


def _badge_text(b, on) -> str:
    """The badge's visible text (for widths)."""
    if BADGE_STYLE.get(b) == ("badge",):
        return BADGE_TEXT if on else BADGE_PLAIN
    return b


def _box(r, sel, applied, on):
    if r["class"] == "blocked":
        return " - "
    res = (applied.get(str(r["id"])) or {}).get("result")
    if res == "applied":
        return _c("[✓]", "bold", on=on)
    if res in ("failed", "started"):
        return _c("[!]", "bold", on=on)
    return "[x]" if r["id"] in sel else _c("[ ]", "dim", on=on)


def _ranges(ids) -> str:
    ids = sorted(ids)
    out, start, prev = [], None, None
    for i in ids:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = i
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else str(start))
    return ",".join(out)


def _runs(items, expand):
    """Split [(row, view)] into runs of similar consecutive effects (collapsed unless expand)."""
    out, cur = [], []
    for it in items:
        r, d = it
        if cur and not expand and d["group"] == cur[-1][1]["group"] and r["id"] == cur[-1][0]["id"] + 1 \
                and not r.get("children") and not cur[-1][0].get("children"):
            cur.append(it)
            continue
        if cur:
            out.append(cur)
        cur = [it]
    if cur:
        out.append(cur)
    final = []
    for run in out:
        if len(run) >= COLLAPSE_MIN:
            final.append(run)
        else:
            final.extend([x] for x in run)
    return final


def _example(d) -> str:
    """One member of a collapsed group, without the verb the group line already says."""
    if d["class"] == "file":
        return f"{d['source']} -> {d['object']}" if d.get("source") else str(d.get("object"))
    if d["class"] == "http":
        return str(d.get("object"))
    if d["class"] in ("outbound", "state", "unknown"):
        return str(d.get("object"))
    return d["summary"]


def _group_text(run) -> str:
    d0 = run[0][1]
    n = len(run)
    cls = d0["class"]
    if cls == "file":
        plus = sum(d.get("added") or 0 for _, d in run)
        minus = sum(d.get("removed") or 0 for _, d in run)
        stat = f" (+{plus} -{minus})" if plus or minus else ""
        return f"{d0['verb']} {n} files{stat}"
    if cls == "http":
        return f"{n} {d0['verb']} requests"
    if cls in ("outbound", "state", "unknown"):
        return f"{n} calls to {d0['tool']}"
    if cls == "command":
        return f"{n} commands"
    return f"{n} × {d0['kind']}"


class _Ctx:
    def __init__(self, rows, sel, applied, on, width, show_diffs, max_diff_lines, edges, expand):
        self.rows, self.sel, self.applied, self.on, self.w = rows, sel, applied, on, width
        self.show_diffs, self.max_diff_lines, self.expand = show_diffs, max_diff_lines, expand
        self.by_id = {r["id"]: r for r in rows}
        self.deps = {}
        for e in edges:
            self.deps.setdefault(e["to"], []).append(e)
        self.collapsed = False


def _detail(ctx, r, pad):
    """Detail lines under one effect, each at most ctx.w columns."""
    on, room = ctx.on, ctx.w - len(pad)
    lines = []

    def add(text, *style, cut=False):
        text = _clean(text)
        for ln in ([fit(text, room)] if cut else wrap(text, room)):
            lines.append(pad + _c(ln, *style, on=on))

    def text_block(prefix, text, limit=MAX_TEXT_LINES):
        src = str(text).splitlines() or [""]
        cont = prefix.rstrip() + " " * (len(prefix) - len(prefix.rstrip()) + 1)  # a wrapped line's rest: "|  "
        shown = total = 0
        for ln in src:
            for k, piece in enumerate(wrap(_clean(ln), room - len(cont))):
                total += 1
                if shown < limit:
                    lines.append(pad + (cont if k else prefix) + piece)
                    shown += 1
        if total > limit:
            add(f"… {total - limit} more lines (--json for all)", "dim")

    p = r.get("preview") or {}
    t = r.get("target") or {}
    cls = r["class"]
    if cls == "file" and t.get("requested_path") and str(t["requested_path"]) not in str(r.get("summary") or ""):
        add(f"named as {t['requested_path']} (a symlink: the change lands at the path above)", "bold")
    if cls == "file" and ctx.show_diffs and p.get("diff"):
        dl = str(p["diff"]).splitlines()
        for ln in dl[2:2 + ctx.max_diff_lines]:
            col = () if ln.startswith("+") and not ln.startswith("+++") else ("dim",)
            add(ln, *col, cut=True)
        if len(dl) - 2 > ctx.max_diff_lines:
            add(f"… {len(dl) - 2 - ctx.max_diff_lines} more diff lines (--json for all)", "dim")
    elif cls == "http":
        req = p.get("request") or {}
        add(f"{req.get('method')} {req.get('url')}")
        for h in req.get("headers") or []:
            add(h, "dim", cut=True)
        if req.get("body"):
            text_block("| ", req["body"])
    elif cls == "command":
        code = str(p.get("code") or "").splitlines() if r.get("tool") == "execute_code" else []
        for ln in code[:MAX_TEXT_LINES]:
            add("  " + ln, "dim", cut=True)
        if len(code) > MAX_TEXT_LINES:
            add(f"  … {len(code) - MAX_TEXT_LINES} more lines of code (--json for all)", "dim")
        _partial_lines(p.get("partial_changes_discarded"), add, lines, pad, room, on)
        out = str(p.get("stdout") or "").splitlines()
        if out:
            for ln in (out[-5:] if r.get("previewable", True) is False else out[:6]):
                add("> " + ln, "dim", cut=True)
    elif cls == "blocked":
        tried = _target_text(t)
        if tried:
            add("tried: " + tried)
        add(str(p.get("error")), "bold")
    else:  # outbound, state, unknown, and any class added later
        if p.get("recipients"):
            add("to: " + ", ".join(map(str, p["recipients"])))
        if p.get("text"):
            text_block("| ", p["text"])
        elif p.get("args"):
            add(json.dumps(p["args"], ensure_ascii=False, default=str), "dim", cut=True)
        if not (r.get("apply") or {}).get("appliable", True):
            add("not appliable: " + str((r.get("apply") or {}).get("reason")), "bold")
    for e in ctx.deps.get(r["id"], []):
        if e["sure"]:
            add(f"uses the output of #{e['from']}", "dim")
        else:
            add(f"may use the output of #{e['from']} (guess: {e['why']})", "dim")
    return lines


def _target_text(t) -> str:
    """What a row's target names, in one line ("" when it names nothing)."""
    if not isinstance(t, dict):
        return ""
    kind = t.get("kind")
    if kind in ("file", "link", "dir") or (kind is None and t.get("path")):
        via = f" (named as {t['requested_path']})" if t.get("requested_path") else ""
        return f"{t.get('path') or t.get('abs_path')}{via}"
    if kind == "command":
        return f"`{' '.join(str(t.get('command') or '').split())}`"
    if kind == "http":
        return f"{t.get('method') or 'GET'} {t.get('url')}"
    if kind == "tool":
        rec = ", ".join(map(str, t.get("recipients") or []))
        return f"{t.get('name')}" + (f" -> {rec}" if rec else "")
    return ", ".join(f"{k}={v}" for k, v in t.items() if isinstance(v, (str, int)) and k != "kind")


def _partial_lines(partial, add, lines, pad, room, on):
    """A can't-preview command's file changes seen in the sandbox before they were thrown away."""
    if not partial or not isinstance(partial, list):
        return
    by = {}
    for c in partial:
        if isinstance(c, dict):
            by.setdefault(str(c.get("change") or "changed"), []).append(str(c.get("path")))
    verbs = {"created": "create", "modified": "modify", "deleted": "delete", "renamed": "move", "mode": "chmod"}
    parts = [f"would {verbs.get(k, k)} {_plural(len(v), 'file')}" for k, v in by.items()]
    add("seen in the sandbox, then discarded (not previewed):", "bold")
    lines.extend(pad + _c(ln, "bold", on=on) for ln in wrap_parts(parts, room))
    for k, v in list(by.items())[:3]:
        add(f"  e.g. {verbs.get(k, k)} {v[0]}" + (f" (+{len(v) - 1} more)" if len(v) > 1 else ""), "dim")
    if len(partial) >= 50:
        add("  (the first 50 changes were recorded)", "dim")


def _effect_lines(ctx, r, d, indent, idw):
    on = ctx.on
    box = _box(r, ctx.sel, ctx.applied, on)
    pad = indent + " " * (3 + 1 + idw + 2)
    room = ctx.w - len(pad)
    head = wrap(_clean(d["summary"]), room)
    lines = [f"{indent}{box} {str(r['id']):>{idw}}  {head[0]}"] + [pad + h for h in head[1:]]
    lines += _badge_lines(ctx, d["badges"], lines, pad)
    lines += _detail(ctx, r, pad)
    return lines


def _badge_lines(ctx, badges, lines, pad):
    """Badges go at the end of the last line when they fit, else on lines of their own."""
    if not badges:
        return []
    plain = " ".join(_badge_text(b, ctx.on) for b in badges)
    styled = " ".join(_badge(b, ctx.on) for b in badges)
    last = lines[-1]
    if vwidth(_strip(last)) + 2 + vwidth(plain) <= ctx.w:
        lines[-1] = last + "  " + styled
        return []
    out, cur, cur_w = [], "", 0
    room = ctx.w - len(pad)
    for b in badges:
        bw = vwidth(_badge_text(b, ctx.on))
        s = _badge(b, ctx.on)
        if cur and cur_w + 1 + bw > room:
            out.append(pad + cur)
            cur, cur_w = "", 0
        if bw > room:
            s = _badge(fit(b, room - 2), ctx.on)
            bw = room
        cur, cur_w = (cur + " " + s, cur_w + 1 + bw) if cur else (s, bw)
    if cur:
        out.append(pad + cur)
    return out


def _strip(s):
    return _SGR_RE.sub("", s)


def _group_lines(ctx, run, indent, idw):
    on = ctx.on
    r0, d0 = run[0]
    ids = [r["id"] for r, _ in run]
    label = _ranges(ids)
    box = _box(r0, ctx.sel, ctx.applied, on)
    pad = indent + " " * (3 + 1 + idw + 2)
    room = ctx.w - len(pad)
    head = wrap(_group_text(run), room)
    lines = [f"{indent}{box} {label:>{idw}}  {head[0]}"] + [pad + h for h in head[1:]]
    lines += _badge_lines(ctx, d0["badges"], lines, pad)
    for r, d in run[:2]:
        for ln in wrap(_clean("e.g. " + _example(d)), room):
            lines.append(pad + _c(ln, "dim", on=on))
    n_dep = sum(1 for r, _ in run if ctx.deps.get(r["id"]))
    tail = f"… {len(run) - 2} more" + (f"; {n_dep} use earlier effects" if n_dep else "")
    for ln in wrap(tail + " (--expand lists them)", room):
        lines.append(pad + _c(ln, "dim", on=on))
    ctx.collapsed = True
    return lines


def _block(ctx, items, indent):
    """Render a list of (row, view) at one level, collapsing runs; children follow their command."""
    runs = _runs(items, ctx.expand)
    labels = [(_ranges([r["id"] for r, _ in run]) if len(run) > 1 else str(run[0][0]["id"])) for run in runs]
    idw = max([3] + [len(x) for x in labels])
    idw = min(idw, 9)
    out = []
    for run in runs:
        if len(run) > 1:
            out.extend(_group_lines(ctx, run, indent, idw))
            continue
        r, d = run[0]
        out.extend(_effect_lines(ctx, r, d, indent, idw))
        kids = [(ctx.by_id[c], describe(ctx.by_id[c])) for c in r.get("children") or []
                if c in ctx.by_id and not ctx.by_id[c].get("vcs_internal")]
        if kids:
            out.extend(_block(ctx, kids, indent + "  "))
        vcs = [x for x in r.get("children") or [] if x in ctx.by_id and ctx.by_id[x].get("vcs_internal")]
        if vcs:
            pad = indent + " " * (3 + 1 + idw + 2)
            for ln in wrap(f"+ {len(vcs)} git internal changes (applied with this command)", ctx.w - len(pad)):
                out.append(pad + _c(ln, "dim", on=ctx.on))
    return out


def render_text(rid: str, show_diffs: bool = True, color=None, max_diff_lines: int = 40, width=None,
                expand: bool = False) -> str:
    meta, rows, applied = load_rehearsal(rid)
    on = _use_color() if color is None else color
    w = _width(width)
    sel, _ = select(rows)
    try:
        edges = dependencies(rows, state.rehearsal_dir(rid), meta.get("workspace"))
    except Exception:
        edges = [{"from": d, "to": r["id"], "sure": True, "why": "recorded"} for r in rows
                 for d in r.get("depends_on") or []]
    t = tally(rows, applied)
    out = [_c(fit(f"{names.PRODUCT}  {meta.get('id', rid)}", w), "bold", on=on)]
    if meta.get("task"):
        task = wrap(_clean(" ".join(str(meta["task"]).split())), w - 6)
        if len(task) > 2:
            task = task[:1] + [fit(task[1] + " " + task[2], w - 6)]
        out.extend(("task  " if i == 0 else "      ") + ln for i, ln in enumerate(task))
    if meta.get("workspace"):
        out.append("in    " + fit_left(_clean(meta["workspace"]), w - 6))
    bits = []
    if meta.get("model"):
        bits.append(_clean(meta["model"]))
    if meta.get("model_calls") is not None:
        bits.append(_plural(meta["model_calls"], "model call"))
    if meta.get("tool_calls") is not None:
        bits.append(_plural(meta["tool_calls"], "tool call"))
    if meta.get("cost_usd") is not None:
        bits.append(f"${meta['cost_usd']:.4f}")
    if meta.get("seconds") is not None:
        bits.append(f"{meta['seconds']:.1f} s")
    out.extend(_c(ln, "dim", on=on) for ln in wrap_parts(bits, w))
    if meta.get("status") not in ("finished", None):
        out.append(_c(fit(f"status: {meta.get('status')}", w), "bold", on=on))
    out.append("")
    happened = f"{t['applied']} applied" if t["applied"] else "0 happened yet"
    head = [_plural(t["effects"], "effect"), f"{t['irreversible']} irreversible", happened]
    for i, ln in enumerate(wrap_parts(head, w)):
        out.append(_c(ln, "bold", on=on) if i == 0 else ln)
    out.extend(wrap_parts(class_parts(t), w))
    unt = t["effects"] - t["ticked_default"]
    out.extend(_c(ln, "dim", on=on) for ln in wrap_parts(
        [f"ticked {t['ticked_default']}"] + ([f"unticked {unt}"] if unt else []) +
        ([f"can't preview {t['cant_preview']}"] if t["cant_preview"] else []), w))
    out.append("")
    ctx = _Ctx(rows, sel, applied, on, w, show_diffs, max_diff_lines, edges, expand)
    top = [(r, describe(r)) for r in rows if not r.get("parent_id") and not r.get("vcs_internal")]
    if not top:
        out.append("  (no effects: the run would change nothing)")
    out.extend(_block(ctx, top, ""))
    out.append("")
    out.extend(_c(ln, "bold", on=on) for ln in wrap_parts(summary_line(rows, applied).split(" · "), w))
    held = [r["id"] for r in rows if r.get("previewable", True) is False and r["class"] != "blocked"
            and r["id"] not in sel and (r.get("apply") or {}).get("appliable")]
    if held:
        out.extend(_c(ln, "dim", on=on) for ln in wrap(
            f"{_plural(len(held), 'effect')} without a preview {'is' if len(held) == 1 else 'are'} unticked; "
            f"they run for real only if you name them, e.g. --only {held[0]}.", w))
    out.append(fit(f"apply:  hermes {names.CLI_NAME} apply {meta.get('id', rid)}", w))
    out.append(fit("choose: --only 1,2  --except 3  (numbers above)", w))
    if ctx.collapsed:
        out.append(fit(f"list every effect: hermes {names.CLI_NAME} report --expand", w))
    return "\n".join(out)


# ---------------------------------------------------------------- compact card (chat sessions)
def render_card(rid: str, max_rows: int = 6, width=None) -> str:
    """A short plain-text card for a chat session (`/clotho report`): counts, the first effects
    (similar ones collapsed), and the two commands. No colour: chat platforms render plain text."""
    meta, rows, applied = load_rehearsal(rid)
    w = _width(width)
    sel, _ = select(rows)
    t = tally(rows, applied)
    happened = f"{t['applied']} applied" if t["applied"] else "nothing has happened yet"
    out = [fit(f"{names.PRODUCT} {meta.get('id', rid)}", w)]
    out.extend(wrap_parts([_plural(t["effects"], "effect"), f"{t['irreversible']} irreversible", happened], w))
    out.extend(wrap_parts(class_parts(t), w)[:2])
    top = [(r, describe(r)) for r in rows if not r.get("parent_id") and not r.get("vcs_internal")]
    shown = 0
    for run in _runs(top, False):
        if shown >= max_rows:
            break
        r, d = run[0]
        if len(run) > 1:
            label, text = _ranges([x["id"] for x, _ in run]), _group_text(run)
        else:
            label, text = str(r["id"]), d["summary"]
        box = "[x]" if r["id"] in sel else "[ ]"
        flag = " !" if d["irreversible"] else ""
        out.append(fit(f"{box} {label} {text}{flag}", w))
        shown += 1
    rest = len(_runs(top, False)) - shown
    if rest > 0:
        out.append(f"+ {rest} more in the full report")
    if t["irreversible"]:
        out.append("! = irreversible")
    out.append(fit(f"review: hermes {names.CLI_NAME} report {meta.get('id', rid)}", w))
    out.append(fit(f"apply:  hermes {names.CLI_NAME} apply {meta.get('id', rid)}", w))
    return "\n".join(out)


def post_card(ctx, rid: str, handle=None):
    """Post the card into the calling chat session, or edit an earlier post in place. Never raises
    (post_message refuses outside a live session, e.g. in cron, and returns a handle saying so)."""
    text = render_card(rid)
    try:
        if handle is not None:
            handle.edit(text)
            return handle
        return ctx.post_message(text)
    except Exception:
        return None


# ---------------------------------------------------------------- apply receipt
def receipt_line(rc: dict) -> str:
    if rc.get("status") == "drift":
        names_ = ", ".join(f"{d['path']} {d.get('why') or 'changed since the rehearsal'}" for d in rc.get("drift", []))
        return f"stopped before any change: {names_} · applied 0 · {rc.get('model_calls', 0)} model calls"
    if rc.get("status") == "dependency":
        return f"stopped before any change · applied 0 · {rc.get('model_calls', 0)} model calls"
    if rc.get("status") == "dry-run":
        return f"dry run: {len(rc.get('selected', []))} effects would apply · no drift"
    s = f"applied {rc['applied']}/{rc['total']} · skipped {rc['skipped']}"
    if rc.get("already"):
        s += f" · already applied {rc['already']}"
    if rc.get("failed"):
        s += f" · failed {rc['failed']}"
    if rc.get("not_attempted"):
        s += f" · not attempted {len(rc['not_attempted'])}"
    if rc.get("held_ids"):
        s += f" · held {len(rc['held_ids'])}"
    return s + f" · {rc['model_calls']} model calls · {rc['seconds']:.1f} s"


def _conflict_lines(rc, w, on, key="conflicts"):
    out = []
    for c in rc.get(key) or []:
        out.extend("  " + ln for ln in wrap(f"#{c['id']} {_clean(c['summary'])}", w - 2))
        out.extend("     " + ln for ln in wrap(f"needs #{c['needs']} {_clean(c['needs_summary'])} (unticked)", w - 5))
        out.extend("     " + _c(ln, "dim", on=on) for ln in
                   wrap(("sure: " if c["sure"] else "guess: ") + _clean(c["why"]), w - 5))
    return out


def render_receipt(rc: dict, color=None, width=None) -> str:
    on = _use_color() if color is None else color
    w = _width(width)
    out = []
    if rc.get("status") == "dependency":
        out.extend(_c(ln, "bold", on=on) for ln in
                   wrap("stopped before any change: ticked effects need unticked ones", w))
        out.extend(_conflict_lines(rc, w, on))
        out.extend(wrap("tick those too, untick the effects that need them, or add --force to apply anyway.", w))
    elif rc.get("conflicts"):
        out.extend(_c(ln, "bold", on=on) for ln in
                   wrap("applying anyway (--force): ticked effects need unticked ones", w))
        out.extend(_conflict_lines(rc, w, on))
    if rc.get("guesses"):
        out.extend(_c(ln, "bold", on=on) for ln in wrap(
            "warning (a guess, not a stop): ticked effects may use the output of unticked ones", w))
        out.extend(_conflict_lines(rc, w, on, key="guesses"))
    if rc.get("status") == "drift":
        for d in rc["drift"]:
            out.extend(_c(ln, "bold", on=on) for ln in
                       wrap(f"drift: {_clean(d['path'])} {_clean(d.get('why') or 'changed since the rehearsal')}", w))
    res = rc.get("results", [])
    i = 0
    while i < len(res):
        r = res[i]
        j = i
        while j + 1 < len(res) and res[j + 1]["result"] == r["result"] and res[j + 1]["id"] == res[j]["id"] + 1 \
                and r["result"] not in ("failed", "held"):
            j += 1
        mark = {"applied": "✓", "failed": "✗", "held": "!"}.get(r["result"], "·")
        if j - i + 1 >= COLLAPSE_MIN:
            out.append(fit(f"  {mark} {res[i]['id']}-{res[j]['id']}  {r['result']} ({j - i + 1} effects)", w))
        else:
            for x in res[i:j + 1]:
                head = wrap(f"{_clean(x['summary'])}  ({x['result']})", w - 9)
                out.append(f"  {mark} {x['id']:>3}  {head[0]}")
                out.extend("         " + h for h in head[1:])
                if x["result"] in ("failed", "held") and x.get("detail"):
                    out.extend("         " + _c(ln, "bold", on=on) for ln in wrap(_clean(x["detail"]), w - 9)[:6])
        i = j + 1
    if rc.get("not_attempted"):
        out.extend(_c(ln, "bold", on=on) for ln in
                   wrap(f"not attempted (after the failure): #{_ranges(rc['not_attempted'])}", w))
    for k, why in (rc.get("not_appliable") or {}).items():
        out.extend(_c(ln, "dim", on=on) for ln in wrap(f"  · {k:>3}  not appliable: {_clean(why)}", w))
    out.extend(receipt_lines(rc, w, on))
    return "\n".join(out)


def receipt_lines(rc: dict, w: int, on: bool) -> list:
    """The receipt's last line(s), bold; on an apply that ran, its "0 model calls" is the one red number."""
    parts = receipt_line(rc).split(" · ")
    ran = rc.get("status") not in ("drift", "dependency", "dry-run")
    key = f"{rc.get('model_calls')} model calls"
    styled = [_c(p, "red", on=on) if ran and p == key and rc.get("model_calls") == 0 else _c(p, "bold", on=on)
              for p in parts]
    sep = _c(" · ", "bold", on=on)
    return wrap_parts(styled, w, sep=sep)


def render_list(limit=20) -> str:
    root = state.data_root()
    ids = sorted((d for d in os.listdir(root) if d.startswith(names.RID_PREFIX)), reverse=True)[:limit]
    lines = []
    for i in ids:
        try:
            meta, rows, applied = load_rehearsal(i)
        except Exception:
            continue
        lines.append(f"{i}  {meta.get('status', '?'):9}  {summary_line(rows, applied)}  \"{(meta.get('task') or '')[:50]}\"")
    return "\n".join(lines) or "no rehearsals yet"
