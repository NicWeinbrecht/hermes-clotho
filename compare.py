"""`hermes clotho compare <fork id>`: the timelines of a fork side by side, one block per timeline, then
the differences between their ledgers in plain words ("only in deepseek-v4-pro: delete 9 files in keep/").

Built on report.describe / report.tally (per-effect and per-run summaries meant for lining rehearsals up),
so a timeline's numbers here are the numbers its own report shows. Same terminal contract as the report
(report.py): lines within 58 columns by default, colour only on a terminal and never with NO_COLOR, bold,
dim and reverse for hierarchy and one hue, red, for the thread you keep: once a timeline of the fork has
been applied, its block is red with "● keep" after its name and the other blocks are dim. Before that no
block is red or dim. The IRREVERSIBLE badge keeps its own look inside a red or dim block.
"""
from __future__ import annotations

import json
import os
import posixpath
import sys
import time
from urllib.parse import urlsplit

from . import fork, names, report

SHOW_DIFFS = 12           # difference lines shown unless --expand
_FILE_VERB_ORDER = ["delete", "move", "create", "modify", "chmod"]


def _leaves(rows):
    """The effects tally counts (a command group counts through its file changes)."""
    return [r for r in rows if not r.get("vcs_internal") and not (r.get("children") and r.get("class") == "command")]


def _folder(d) -> str:
    obj = str(d.get("object") or "")
    if d["class"] == "file":
        parent = posixpath.dirname(obj.rstrip("/"))
        return parent
    if d["class"] == "http":
        try:
            return urlsplit(obj).netloc or obj
        except ValueError:
            return obj
    return ""


def _load_timeline(t: dict) -> dict:
    out = {"name": t["name"], "model": t.get("model"), "provider": t.get("provider"), "rid": t.get("rid"),
           "status": t.get("status"), "reason": t.get("reason"), "seconds": t.get("seconds"),
           "model_calls": t.get("model_calls"), "aux_calls": t.get("aux_calls"), "cap": t.get("cap"),
           "capped_calls": t.get("capped_calls"), "cost_usd": t.get("cost_usd"),
           "cost_usd_incl_aux": t.get("cost_usd_incl_aux"), "model_switched": t.get("model_switched"),
           "plan": fork.is_plan_provider(t.get("provider")),
           "instructions": t.get("instructions") or (t.get("prompt") if t.get("own_prompt") else None),
           "rows": [], "effects": []}
    if t.get("rid"):
        try:
            meta, rows, applied = report.load_rehearsal(t["rid"])
        except (OSError, ValueError):
            meta, rows, applied = {}, [], {}
        out["rows"] = rows
        out["tally"] = report.tally(rows, applied)
        out["effects"] = [report.describe(r) for r in _leaves(rows)]
        out["tool_calls"] = meta.get("tool_calls")
        out["meta_status"] = meta.get("status")
        if out["model_calls"] is None:
            out["model_calls"] = meta.get("model_calls")
        if out["cost_usd"] is None:
            out["cost_usd"] = meta.get("cost_usd")
    else:
        out["tally"] = report.tally([])
    verbs = {}
    for d in out["effects"]:
        if d["class"] == "file":
            verbs[d["verb"]] = verbs.get(d["verb"], 0) + 1
    out["file_verbs"] = verbs
    return out


SHOW_NAMES = 3           # file names listed after a group of deletes
CMD_CHARS = 100          # a command in a difference line is cut here (the timeline's report has it in full)


def _phrase(cls, kind, verb, folder, items, top="./") -> str:
    n = len(items)
    d0 = items[0]
    if cls == "file":
        where = f"{folder}/" if folder else top
        if n == 1:
            if d0.get("source"):
                return f"move {report._clean(d0['source'])} -> {report._clean(d0['object'])}"
            return f"{verb} {report._clean(d0['object'])}"
        if verb == "move":
            return f"move {n} files into {where}"
        text = f"{verb} {n} files in {where}"
        if verb == "delete":  # name the first few: a delete is what a reader checks first
            base = sorted(posixpath.basename(str(d.get("object") or "")) for d in items)
            text += ": " + ", ".join(report._clean(b) for b in base[:SHOW_NAMES]) + \
                    (f" +{n - SHOW_NAMES}" if n > SHOW_NAMES else "")
        return text
    if cls == "http":
        if n == 1:
            return f"{verb} {report._clean(d0['object'])}"
        return f"{n} {verb} requests to {folder}"
    if cls == "command":
        if n == 1:
            return f"run `{report.fit(report._clean(' '.join(str(d0.get('object') or '').split())), CMD_CHARS)}`"
        return f"{n} commands"
    if n == 1:
        return f"{kind}: {report._clean(d0.get('object') or d0.get('tool'))}"
    return f"{n} × {kind} ({report._clean(d0.get('tool'))})"


def _who(names_in, all_names) -> str:
    if len(names_in) == 1:
        return f"only in {names_in[0]}"
    missing = [n for n in all_names if n not in names_in]
    if len(missing) == 1 and len(all_names) > 2:
        return f"not in {missing[0]}"
    return "only in " + ", ".join(names_in)


def differences(tls: list, top: str = "./") -> dict:
    """Effects present in some timelines and not others, grouped and phrased; plus same-target effects whose
    content differs. Pure: works on the loaded timelines."""
    live = [t for t in tls if t.get("rid") and t["status"] in ("finished", "capped")]
    names_all = [t["name"] for t in live]
    keyed = {}   # key -> {timeline: [descr, ...]}
    for t in live:
        for d in t["effects"]:
            keyed.setdefault(d["key"], {}).setdefault(t["name"], []).append(d)
    groups, content = {}, {}
    common = 0
    for key, per in keyed.items():
        present = [n for n in names_all if n in per]
        if len(present) < len(names_all):
            for n in present:
                for d in per[n]:
                    g = (tuple(present), d["class"], d["kind"], d.get("verb"), _folder(d) if d["class"] != "command"
                         else d.get("object"))
                    groups.setdefault(g, {}).setdefault(n, []).append(d)
            continue
        counts = {n: len(per[n]) for n in present}
        payloads = {n: tuple(sorted(str(d.get("payload")) for d in per[n])) for n in present}
        if len(set(counts.values())) > 1 or len(set(payloads.values())) > 1:
            d0 = per[present[0]][0]
            g = (d0["class"], d0["kind"], d0.get("verb"))
            content.setdefault(g, []).append({"key": key, "d": d0, "counts": counts,
                                              "same_content": len(set(payloads.values())) == 1})
        else:
            common += 1
    out = []
    for (present, cls, kind, verb, folder), per in groups.items():
        items = per[present[0]]   # the same effects (by key) in every timeline listed; show the first's
        irr = any(d["irreversible"] for ds in per.values() for d in ds)
        text = f"{_who(list(present), names_all)}: {_phrase(cls, kind, verb, folder if cls != 'command' else '', items, top)}"
        if len(present) > 1 and len({tuple(sorted(str(d.get("payload")) for d in ds)) for ds in per.values()}) > 1:
            text += ", different content"
        out.append({"timelines": list(present), "class": cls, "kind": kind, "verb": verb,
                    "where": folder if cls != "command" else None, "count": len(items),
                    "irreversible": irr, "keys": sorted({d["key"] for d in items}), "text": text})
    for (cls, kind, verb), lst in content.items():
        lst.sort(key=lambda x: str(x["d"].get("object")))
        objs = [report._clean(x["d"].get("object") or x["d"].get("tool")) for x in lst]
        diff_counts = [x for x in lst if len(set(x["counts"].values())) > 1]
        if diff_counts:
            x = diff_counts[0]
            how = ", ".join(f"{n} {c}×" for n, c in x["counts"].items())
            text = f"all: {verb} {objs[0]} ({how})" + (f" and {len(lst) - 1} more" if len(lst) > 1 else "")
        else:
            what = objs[0] if len(objs) == 1 else f"{len(objs)} {kind}s"
            text = f"all: {verb} {what}, different content"
        out.append({"timelines": names_all, "class": cls, "kind": kind, "verb": verb, "where": None,
                    "count": len(lst), "irreversible": any(x["d"]["irreversible"] for x in lst),
                    "keys": [x["key"] for x in lst], "text": text, "same_target_different_content": True})

    def rank(x):
        v = x.get("verb")
        return (0 if v == "delete" else 1 if x["irreversible"] else 2,
                1 if x.get("same_target_different_content") else 0, -x["count"], x["text"])
    out.sort(key=rank)
    return {"differences": out, "identical_in_all": common, "compared": names_all}


def build(fid: str) -> dict:
    rec = fork.load_fork(fid)
    tls = [_load_timeline(t) for t in rec.get("timelines") or []]
    ws = str(rec.get("workspace") or "").rstrip("/")
    diff = differences(tls, top=(posixpath.basename(ws) + "/") if ws else "./")
    return {"fork": rec, "timelines": tls, **diff}


def _money(t) -> str:
    if t["plan"]:
        return "on the plan"
    c = t.get("cost_usd_incl_aux") if t.get("cost_usd_incl_aux") is not None else t.get("cost_usd")
    return f"${c:.4f}" if isinstance(c, (int, float)) else "$?"


KEEP = "● keep"


def _block(t, w, on, row=None) -> list:
    """One timeline's block. ``row``: "red" for the kept timeline, "dim" for the others once one is kept,
    None before any is; a red or dim block carries that one style on every line (and nothing else but the
    IRREVERSIBLE badge)."""
    rs = (row,) if row else ()

    def c(text, *style, on=on):
        return report._c(text, *(rs or style), on=on)

    name = t["name"] + (f"  {KEEP}" if row == "red" else "")
    out = [c(report.fit(name, w), "bold")]
    sub = [report._clean(t["model"] or "default model")]
    if t.get("rid"):
        sub.append(t["rid"])
    out.extend(c(ln, "dim") for ln in report.wrap_parts(sub, w - 2))
    if t.get("instructions"):
        ins = report.wrap(report._clean(" ".join(str(t["instructions"]).split())), w - 4)
        out.append(c(ins[0] if len(ins) == 1 else report.fit(ins[0] + " " + ins[1], w - 4), "dim"))
    out[1:] = ["  " + x for x in out[1:]]
    st = t["status"]
    if st in ("dropped", "refused", "failed", "timed out"):
        out.extend("  " + c(ln, "bold") for ln in report.wrap(f"{st}: {report._clean(t.get('reason') or '')}",
                                                               w - 2)[:4])
        if not t.get("rid"):
            return out
    ta = t["tally"]
    irr = ta["irreversible"]
    head = f"{ta['effects']} effects" if ta["effects"] else "no effects"
    if irr:
        if on and rs:  # the badge resets the block's style and opens it again after itself
            line = report._open(*rs) + head + "  " + report.badge(on, rs) + f" {irr}" + report._close(*rs)
        else:
            line = c(head, "bold") + "  " + report.badge(on) + f" {irr}"
        out.append("  " + line)
    else:
        out.append("  " + c(head, "bold") + "  " + c("0 irreversible", "dim"))
    parts = report.class_parts(ta)
    if parts:
        out.extend("  " + ln for ln in report.wrap_parts(parts, w - 2))
    fv = t["file_verbs"]
    if fv:
        order = _FILE_VERB_ORDER + sorted(set(fv) - set(_FILE_VERB_ORDER))
        out.extend("  " + ln for ln in report.wrap_parts([f"{k} {fv[k]}" for k in order if fv.get(k)], w - 2))
    if ta.get("cant_preview"):
        out.append("  " + c(f"can't preview {ta['cant_preview']}", "bold"))
    calls = t.get("model_calls")
    bits = [report._plural(calls if calls is not None else 0, "model call")]
    if t.get("aux_calls"):
        bits[-1] += f" (+{t['aux_calls']} aux)"
    if t.get("seconds") is not None:
        bits.append(f"{t['seconds']:.1f} s")
    bits.append(_money(t))
    out.extend("  " + c(ln, "dim") for ln in report.wrap_parts(bits, w - 2))
    if st == "capped":
        out.append("  " + c(report.fit(f"stopped at its cap of {t.get('cap')} model calls", w - 2), "bold"))
    if t.get("model_switched"):
        out.extend("  " + c(ln, "bold") for ln in
                   report.wrap("answered by another model too: " + ", ".join(t["model_switched"]), w - 2))
    if rs:  # lines without colour codes of their own still carry the block's style
        out = [x if "\033[" in x else report._c(x, *rs, on=on) for x in out]
    return out


def kept_timeline(rec: dict):
    """The timeline of this fork that was applied (the one kept), or None."""
    applied = [a for a in rec.get("applied") or [] if a.get("applied")]
    return applied[-1]["timeline"] if applied else None


def render_text(data: dict, width=None, color=None, expand=False) -> str:
    on = report._use_color() if color is None else color
    w = report._width(width)
    rec, tls = data["fork"], data["timelines"]
    c = report._c
    out = [c(report.fit(f"{names.PRODUCT} fork  {rec['id']}", w), "bold", on=on)]
    own = [t for t in rec.get("timelines") or [] if t.get("own_prompt")]
    if own and len(own) == len(rec.get("timelines") or []):
        out.append("task  each timeline has its own prompt (below)")
    elif rec.get("task"):
        task = report.wrap(report._clean(" ".join(str(rec["task"]).split())), w - 6)
        if len(task) > 2:
            task = task[:1] + [report.fit(task[1] + " " + task[2], w - 6)]
        out.extend(("task  " if i == 0 else "      ") + ln for i, ln in enumerate(task))
    out.append("in    " + report.fit_left(report._clean(rec.get("workspace") or ""), w - 6))
    how = rec.get("mode") or "at once"
    wall = rec.get("wall_seconds")
    head = [report._plural(len(tls), "timeline"), how] + ([f"{wall:.1f} s"] if isinstance(wall, (int, float)) else [])
    out.extend(c(ln, "dim", on=on) for ln in report.wrap_parts(head, w))
    if rec.get("slept_seconds"):
        out.extend(c(ln, "bold", on=on) for ln in report.wrap(
            f"the computer slept for about {rec['slept_seconds']} s during this fork; a model call that was "
            "waiting then may have failed, and times above leave the sleep out", w))
    if rec.get("status") == "running":
        out.append(c("status: running", "bold", on=on))
    for x in rec.get("collisions") or []:
        out.extend(c(ln, "bold", on=on) for ln in report.wrap(
            f"{x['timeline']} collided when run at once ({report._clean(x.get('reason') or '')}); it ran again "
            "after the others", w))
    kept = kept_timeline(rec)
    for t in tls:
        out.append("")
        out.extend(_block(t, w, on, row=None if not kept else "red" if t["name"] == kept else "dim"))
    diffs = data["differences"]
    out.append("")
    compared = data["compared"]
    if len(compared) < 2:
        out.append(report.fit("nothing to compare: fewer than 2 timelines have a ledger", w))
    elif not diffs:
        out.append(c(report.fit(f"no differences: the {len(compared)} ledgers hold the same effects", w), "bold", on=on))
    else:
        out.append(c(report.fit(f"differences ({len(diffs)})", w), "bold", on=on))
        shown = diffs if expand else diffs[:SHOW_DIFFS]
        for d in shown:
            text = d["text"] + (" · irreversible" if d["irreversible"] else "")
            lines = report.wrap(text, w - 4)
            out.append("  " + lines[0])
            out.extend("    " + ln for ln in lines[1:])
        if len(diffs) > len(shown):
            out.append(c(report.fit(f"+ {len(diffs) - len(shown)} more: compare {rec['id']} --expand", w), "dim",
                         on=on))
    if len(compared) >= 2:
        out.append(c(report.fit(f"same in all {len(compared)}: {report._plural(data['identical_in_all'], 'effect')}",
                                w), "dim", on=on))
    left_out = [t for t in tls if t["name"] not in compared and t["status"] != "dropped"]
    if left_out and len(compared) >= 1:
        out.extend(c(ln, "dim", on=on) for ln in report.wrap(
            "not compared: " + ", ".join(f"{t['name']} ({t['status']})" for t in left_out), w))
    dropped = [t for t in tls if t["status"] == "dropped"]
    if dropped:
        out.extend(c(ln, "bold", on=on) for ln in report.wrap(
            "dropped (cannot drive the tool loop): " + ", ".join(t["name"] for t in dropped), w))
    ok = [t for t in tls if t.get("rid") and t["status"] in ("finished", "capped")]
    applied = [a for a in rec.get("applied") or [] if a.get("applied")]
    out.append("")
    if applied:
        out.append(report.fit(f"applied: {applied[-1]['timeline']} ({applied[-1].get('status')})", w))
    if ok:
        out.append("apply one:")
        for t in ok:
            cmd = f"hermes {names.CLI_NAME} apply"
            spec = f"{rec['id']}/{t['name']}"
            if report.vwidth(f"  {cmd} {spec}") <= w:
                out.append(f"  {cmd} {spec}")
            else:  # still one shell command when copied: a continuation line, never a cut
                out.append(f"  {cmd} \\")
                out.append("    " + (spec if report.vwidth(spec) <= w - 4 else report._clean(spec)))
    out.append(c(report.fit("0 things happened yet in any timeline" if not applied else
                            "the other timelines were not applied", w), "dim", on=on))
    return "\n".join(out)


def to_json(data: dict) -> dict:
    tls = []
    for t in data["timelines"]:
        x = {k: v for k, v in t.items() if k not in ("rows", "effects")}
        x["effects"] = [{k: v for k, v in d.items() if k != "group"} for d in t["effects"]]
        tls.append(x)
    return {"fork": data["fork"], "timelines": tls, "differences": data["differences"],
            "identical_in_all": data["identical_in_all"], "compared": data["compared"]}


def _as_the_demo_showed(data: dict) -> dict:
    """The demo's own fork, compared again later: count its calls as the demo screen did (demo.shown_counts).
    A capped recording ends with a stop the demo gives itself ("cap stop" in its replay log), which is no model
    call. Only for a fork whose every timeline was answered by the demo's recording and whose replay logs are
    still next to its workspace; the stored counts are not changed."""
    from . import demo
    tls = data.get("timelines") or []
    if not tls or any(t.get("provider") != demo.REPLAY_PROVIDER for t in tls):
        return data
    ws = str((data.get("fork") or {}).get("workspace") or "")
    stage = os.path.join(os.path.dirname(os.path.dirname(ws)), "stage")
    try:
        with open(os.path.join(stage, "plan.json")) as f:
            if json.load(f).get("workspace") != ws:
                return data
        per = demo._replay_stats(os.path.join(stage, "replay"))["per_timeline"]
    except (OSError, ValueError, AttributeError):
        return data
    return demo.shown_counts(data, per)


def print_compare(fid: str, as_json=False, width=None, expand=False, stream=None) -> int:
    stream = stream or sys.stdout
    t0 = time.monotonic()
    data = _as_the_demo_showed(build(fid))
    if as_json:
        js = to_json(data)
        js["render_seconds"] = round(time.monotonic() - t0, 4)
        stream.write(json.dumps(js, indent=1, ensure_ascii=False, default=str) + "\n")
    else:
        stream.write(render_text(data, width=width, expand=expand) + "\n")
    return 0
