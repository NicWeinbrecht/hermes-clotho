"""`/clotho on | off | report` inside a normal session (best effort; see the Disclosure in plugin.yaml).

The armed state lives only in this process's memory, keyed by the session id: if the plugin is not
loaded (import failure, gateway restart), nothing is armed and nothing reads a stale flag.
"""
from __future__ import annotations

import os

from . import capture, names, report, state
from .ledger import counts, finalize as finalize_ledger


def handle(raw_args: str = "", session_id: str = "", **kw) -> str:
    try:
        parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else "status"
        key = session_id or kw.get("session_key") or ""
        live = state.session_rehearsal(key) if key else None
        if sub == "on":
            if live is not None:
                return f"{names.PRODUCT} is already on for this session ({live.id})."
            if not key:
                return f"{names.PRODUCT}: no session id available here; use `hermes {names.CLI_NAME} run \"<task>\"`."
            ws = None
            try:
                ws = capture._CTX.session_cwd() if capture._CTX is not None else None
            except Exception:
                ws = None
            ws = (parts[1] if len(parts) > 1 else None) or ws
            if not ws:
                return (f"{names.PRODUCT}: this session has no working folder yet; "
                        f"name one: /{names.SLASH_NAME} on <folder>")
            r = state.Rehearsal(state.new_id(), ws, task="(session)", mode="session", session_key=key)
            r.create()
            state.arm_session([key], r)
            return (f"{names.PRODUCT} on ({r.id}) in {r.workspace}. Reads run for real; every effect is captured "
                    f"until /{names.SLASH_NAME} off. Subagents are captured as one effect in session mode.")
        if sub == "off":
            if live is None:
                return f"{names.PRODUCT} is not on in this session."
            with live.lock:
                live.closed = True
                rows = finalize_ledger(live.ledger_path, live.reads)
            state.disarm_session(live)
            live.write_meta(ended=state.now_iso(), status="finished", model_calls=live.model_calls,
                            tool_calls=live.tool_calls, counts=counts(rows), effects=len(rows))
            return (f"{names.PRODUCT} off. {report.summary_line(rows)}\n"
                    f"Review: hermes {names.CLI_NAME} report {live.id}   Apply: hermes {names.CLI_NAME} apply {live.id}")
        if sub == "report":
            rid = live.id if live is not None and len(parts) < 2 else state.resolve_id(parts[1] if len(parts) > 1 else None)
            if live is not None and rid == live.id:
                from .ledger import load
                rows = load(live.ledger_path)
                return f"{names.PRODUCT} {live.id} (still on): {report.summary_line(rows)}\n" + "\n".join(
                    f"  {r['id']:>3}  {r['summary']}" for r in rows if not r.get("parent_id"))
            if not rid:
                return "no rehearsal yet"
            return report.render_card(rid)
        if live is not None:
            return f"{names.PRODUCT} is on ({live.id}) in {live.workspace}."
        return f"{names.PRODUCT} is off. Usage: /{names.SLASH_NAME} on | off | report"
    except state.Refused as exc:
        return f"{names.PRODUCT}: refused: {exc}"
    except Exception as exc:
        return f"{names.PRODUCT}: error: {type(exc).__name__}: {exc}"
