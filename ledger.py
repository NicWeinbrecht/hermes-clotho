"""The effects ledger: one JSON object per effect per line (schema: docs/ledger.schema.json).

Rows are appended while the rehearsal runs. When it ends, ``finalize`` computes dependencies:
  * an effect on a path depends on every earlier effect on the same path;
  * a shell command depends on earlier file effects whose path it names;
  * a message, web request or other outbound / state effect depends on an earlier file effect
    whose path its arguments name, or whose path the agent read in between (read_file,
    search_files, or a sandboxed command naming the path): its content may be built from that
    would-be state, e.g. an email summarising a file the agent just wrote.
``dependents`` is the inverse list ("later effects that read this effect's output").
"""
from __future__ import annotations

import json
import os

from . import names


def append(path: str, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def load(path: str) -> list:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _mentions(text: str, rel: str) -> bool:
    if not text or not rel:
        return False
    if rel in text:
        return True
    base = os.path.basename(rel)
    return len(base) >= 3 and base in text


def finalize(path: str, reads: list) -> list:
    """Fill depends_on / dependents and rewrite the ledger atomically. ``reads`` holds
    (call_seq, kind, detail) for every read the agent made during the rehearsal. A live rehearsal that owns
    this ledger first gets BLOCKED rows for sandbox reports that arrived after their commands returned."""
    try:
        from . import capture, state
        live = state.by_ledger(path)
        if live is not None:
            capture.late_rows(live)
    except Exception:
        pass
    rows = load(path)
    by_id = {r["id"]: r for r in rows}
    file_rows = [r for r in rows if r["class"] == "file"]
    for r in rows:
        deps = set(r.get("depends_on") or [])
        seq = r.get("source", {}).get("call_seq", 0)
        tpath = (r.get("target") or {}).get("path")
        for f in file_rows:
            if f["id"] >= r["id"] or f["id"] == r.get("parent_id"):
                continue
            fpath = f["target"]["path"]
            fseq = f.get("source", {}).get("call_seq", 0)
            if r["class"] == "file" and tpath == fpath:
                deps.add(f["id"]); continue
            if f.get("parent_id") and f.get("parent_id") == r.get("parent_id"):
                continue  # siblings from one command
            if r["class"] in ("command", "http") and _mentions(r.get("args", {}).get("command", ""), fpath):
                deps.add(f["id"]); continue
            if r["class"] in ("outbound", "state", "unknown") and _mentions(
                    json.dumps(r.get("args", {}), ensure_ascii=False), fpath):
                deps.add(f["id"]); continue
            if r["class"] not in ("http", "outbound", "state", "unknown"):
                continue  # only effects that carry content out are tied to what the agent read
            for (rseq, kind, detail) in reads:
                if fseq < rseq < seq and _mentions(detail, fpath):
                    deps.add(f["id"]); break
        r["depends_on"] = sorted(deps)
    for r in rows:
        r["dependents"] = sorted(o["id"] for o in rows if r["id"] in o.get("depends_on", []))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)
    return rows


def counts(rows: list) -> dict:
    c = {"file": 0, "command": 0, "http": 0, "outbound": 0, "state": 0, "unknown": 0, "blocked": 0,
         "irreversible": 0, "cant_preview": 0, "deletes": 0, "creates": 0, "modifies": 0, "renames": 0}
    for r in rows:
        c[r["class"]] = c.get(r["class"], 0) + 1
        if r.get("irreversible"):
            c["irreversible"] += 1
        if not r.get("previewable", True):
            c["cant_preview"] += 1
        ch = r.get("change")
        if r["class"] == "file" and ch:
            key = {"deleted": "deletes", "created": "creates", "modified": "modifies", "renamed": "renames"}.get(ch)
            if key:
                c[key] += 1
    return c


SCHEMA_VERSION = names.LEDGER_SCHEMA
