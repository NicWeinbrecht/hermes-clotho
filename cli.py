"""`hermes clotho ...`: run, report, apply, list, check."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time

from . import apply as apply_mod
from . import capture, fork, names, report, sandbox, state
from . import cron as cron_mode
from . import demo
from .ledger import counts, finalize as finalize_ledger, load


def setup(parser):
    subs = parser.add_subparsers(dest="clotho_cmd")
    p = subs.add_parser("run", help="rehearse a task: every effect is captured, nothing happens")
    p.add_argument("task")
    p.add_argument("-m", "--model", default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--workspace", default=None, help="project folder to rehearse in (default: current folder)")
    p.add_argument("--toolsets", default=None, help="comma-separated toolsets, as for hermes -z")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--no-diffs", action="store_true")
    _report_args(p)
    p = subs.add_parser("report", help="show the effect report of a rehearsal")
    p.add_argument("id", nargs="?", default="last")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-diffs", action="store_true")
    _report_args(p)
    p = subs.add_parser("apply", help="replay the ticked effects for real, with no model calls")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--only", default="", help="apply only these effect numbers, e.g. 1,2,5")
    p.add_argument("--except", dest="except_", default="", help="skip these effect numbers, e.g. 3")
    p.add_argument("--dry-run", action="store_true", help="check selection, dependencies and drift, change nothing")
    p.add_argument("--force", action="store_true",
                   help="apply even when a ticked effect needs an unticked one (drift still stops it)")
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("list", help="list rehearsals")
    p.add_argument("--limit", type=int, default=20)
    p = subs.add_parser("check", help="self-check: can a rehearsal be armed in this process?")
    p.add_argument("--provider", default=None, help="check this provider instead of the configured one")
    cron_mode.add_parser(subs)
    fork.add_parsers(subs)
    demo.add_parser(subs)
    parser.set_defaults(func=handle)


def _report_args(p):
    p.add_argument("--expand", action="store_true", help="list every effect; similar ones are grouped by default")
    p.add_argument("--width", type=int, default=None,
                   help=f"columns per line (default {report.WIDTH}; 0 = this terminal's width)")


def _ids(text):
    out = []
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def selfcheck(provider=None) -> list:
    """Reasons a rehearsal must not be armed in this process (empty = safe)."""
    problems = []
    try:
        from hermes_cli.plugins import _delivery_manager
        chain = list(_delivery_manager()._middleware.get("tool_execution", []))
        if not chain or chain[0] is not capture.tool_execution:
            first = getattr(chain[0], "__module__", "?") + "." + getattr(chain[0], "__name__", "?") if chain else "none"
            problems.append(f"{names.PRODUCT}'s tool middleware is not first in this process (first: {first}); "
                            "another plugin could run tools before it sees them")
        llm = list(_delivery_manager()._middleware.get("llm_execution", []))
        if capture.llm_execution not in llm:
            problems.append("the model-call counter is not registered")
    except Exception as exc:
        problems.append(f"cannot inspect the middleware chain ({type(exc).__name__}: {exc})")
    try:
        from hermes_cli.plugin_isolation import isolation_mode
        if isolation_mode() == "host":
            problems.append("plugins.isolation is 'host': capture across the host boundary is unverified")
    except Exception:
        pass
    name, where = effective_provider(provider)
    why = provider_problem(name, _configured_base_url() if where != "--provider" else "")
    if why:
        problems.append(f"provider '{name}' ({where}) {why}")
    return problems


INFERENCE_ENV = "HERMES_INFERENCE_PROVIDER"


def effective_provider(provider=None):
    """(provider name, where it came from), resolved the way Hermes resolves it for this run:
    --provider, then the configured default (config.yaml model.provider), then
    HERMES_INFERENCE_PROVIDER, else "auto". Every route gets the same check."""
    if provider and str(provider).strip():
        return str(provider).strip().lower(), "--provider"
    env = os.environ.get(INFERENCE_ENV, "").strip().lower()
    try:
        from hermes_cli.runtime_provider import resolve_requested_provider
        name = (resolve_requested_provider(None) or "auto").strip().lower()
    except Exception:
        name = env or "auto"
    if name == "auto":
        return name, "default"
    return name, (INFERENCE_ENV if name == env else "configured default")


def _configured_base_url() -> str:
    try:
        from hermes_cli.runtime_provider import load_config
        m = (load_config() or {}).get("model")
        return str(m.get("base_url") or "") if isinstance(m, dict) else ""
    except Exception:
        return ""


def provider_problem(name, base_url="") -> str:
    """Why this provider must not drive a rehearsal ("" = fine). The rule (D35: a provider that can
    run tools outside Hermes' tool path is refused unless its own tools are verified off) lives in
    capture.provider_problem; until that exists this refuses every such provider, on every route."""
    check = getattr(capture, "provider_problem", None)
    if callable(check):
        try:
            return check(name, base_url=base_url) or ""
        except Exception as exc:  # fail closed
            return f"could not be checked ({type(exc).__name__}: {exc})"
    n = (name or "").lower()
    if "acp" in n or "claude-subscription" in n or "claude-code" in n or str(base_url).startswith("acp"):
        return "runs tools outside Hermes' tool path; a rehearsal cannot capture them"
    return ""


def handle(args):
    cmd = getattr(args, "clotho_cmd", None)
    try:
        if cmd in fork.COMMANDS:
            return fork.handle(args)
        if cmd == "apply" and fork.is_fork_spec(args.id):
            return fork.cmd_apply(args)
        if cmd == "run":
            return cmd_run(args)
        if cmd == "report":
            return cmd_report(args)
        if cmd == "apply":
            return cmd_apply(args)
        if cmd == "cron":
            return cron_mode.handle(args)
        if cmd == "demo":
            return demo.handle(args)
        if cmd == "list":
            print(report.render_list(args.limit))
            return 0
        if cmd == "check":
            probs = selfcheck(getattr(args, "provider", None))
            ok, why = sandbox.available()
            print("armable: " + ("yes" if not probs else "no"))
            for p in probs:
                print("  - " + p)
            print("shell preview: " + ("sandboxed clone" if ok else f"capture only ({why})"))
            print(f"data: {state.data_root()}")
            print(f"python: {sys.executable} (bytecode writes {'off' if sys.dont_write_bytecode else 'on'}, pycache prefix {sys.pycache_prefix})")
            return 0 if not probs else 3
    except state.Refused as exc:
        print(f"{names.PRODUCT}: refused: {exc}", file=sys.stderr)
        return 3
    print(f"usage: hermes {names.CLI_NAME} run|report|apply|list|check|fork|compare|demo   ({names.PITCH})")
    return 2


def cmd_run(args):
    task = args.task
    provider = args.provider
    problems = selfcheck(provider)
    if problems:
        print(f"{names.PRODUCT}: refusing to start the model: nothing would be rehearsed safely.", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 3
    ws = os.path.realpath(args.workspace or os.getcwd())
    r = state.Rehearsal(state.new_id(), ws, task=task, mode="run", model=args.model, provider=provider)
    r.create()
    ok, why = sandbox.available()
    r.write_meta(shell_preview="sandboxed clone" if ok else f"capture only ({why})")
    state.arm_process(r)  # process-wide: subagents and children are covered; disarmed only at exit
    os.chdir(ws)
    os.environ["TERMINAL_CWD"] = ws
    offer_what_a_plain_run_offers()
    usage = os.path.join(r.dir, "usage.json")
    print(f"{names.PRODUCT} {r.id}: rehearsing in {ws} (nothing will happen for real)", file=sys.stderr)
    t0 = time.monotonic()
    buf = io.StringIO()
    from hermes_cli.oneshot import run_oneshot
    rc = 1
    err = None
    try:
        with contextlib.redirect_stdout(buf):
            rc = run_oneshot(task, model=args.model, provider=provider, usage_file=usage,
                             toolsets=args.toolsets.split(",") if args.toolsets else None)
    except BaseException as exc:  # still finalize what was captured
        err = f"{type(exc).__name__}: {exc}"
    finish(r, rc, usage, time.monotonic() - t0, agent_text=buf.getvalue(), error=err)
    if args.json:
        print(json.dumps(report.to_json(r.id), indent=1, ensure_ascii=False, default=str))
    else:
        text = r.swap_out(buf.getvalue()).strip()
        if text:
            print(report._c("agent's last message (written during the rehearsal; nothing has happened yet):",
                            "dim", on=report._use_color()))
            print(text)
            print()
        print(report.render_text(r.id, show_diffs=not args.no_diffs, width=args.width, expand=args.expand))
    return 0 if rc == 0 and not err else (rc or 1)


def offer_what_a_plain_run_offers():
    """A plain `hermes -z` runs its agent in the gateway, and start_gateway() sets HERMES_EXEC_ASK=1; tools
    whose availability check reads it (cronjob_manage) are offered there and nowhere else. `run` calls
    run_oneshot in this process, so without it the model's catalog had one tool fewer (it searched three
    times as long for a cron task). Approvals are not affected: run_oneshot marks the process as a single
    query, which clears the ask flag for approval prompts."""
    os.environ.setdefault("HERMES_EXEC_ASK", "1")


def finish(r: state.Rehearsal, rc, usage_path, seconds, agent_text="", error=None):
    with r.lock:
        r.closed = True  # stays armed: any late call (background jobs, review forks) is blocked
        rows = finalize_ledger(r.ledger_path, r.reads)
    usage = {}
    try:
        usage = json.load(open(usage_path))
    except Exception:
        pass
    r.write_meta(ended=state.now_iso(), status="finished" if rc == 0 and not error else "failed",
                 exit_code=rc, error=error, seconds=round(seconds, 2), model_calls=r.model_calls,
                 tool_calls=r.tool_calls, ran_for_real=r.ran_for_real, observed_commands=r.observed_commands,
                 blocked=r.blocked, counts=counts(rows), effects=len(rows),
                 cost_usd=usage.get("estimated_cost_usd"), usage=usage or None,
                 agent_final_text=(r.swap_out(agent_text or ""))[:4000])


NO_ID = (None, "", "last", "latest")  # what resolve_id reads as "the newest rehearsal"


def _say_err(text):
    for ln in report.wrap(text, report._width(None)):
        print(ln, file=sys.stderr)


def _none_yet(what):
    _say_err(f"no rehearsals yet in this Hermes home: nothing to {what}.")
    _say_err(f'Make one with: hermes {names.CLI_NAME} run "<task>"')


def _apply_line(spec):
    """`hermes clotho apply <spec>` within the width; one shell command even when it continues."""
    cmd, w = f"hermes {names.CLI_NAME} apply", report._width(None)
    if report.vwidth(f"  {cmd} {spec}") <= w:
        return [f"  {cmd} {spec}"]
    return [f"  {cmd} \\", f"    {spec}"]


def _metas():
    root = state.data_root()
    for d in sorted(os.listdir(root)):
        if d.startswith(names.RID_PREFIX):
            try:
                with open(os.path.join(root, d, "meta.json")) as f:
                    yield json.load(f)
            except (OSError, ValueError):
                continue


def not_yours_to_pick(rid) -> list:
    """Lines saying why a bare `apply` must not take ``rid`` (empty: it is a plain rehearsal, go on).
    A fork timeline or a cron-batch run is one of several futures of one task; apply never picks one."""
    try:
        with open(os.path.join(state.rehearsal_dir(rid), "meta.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return []
    fid = meta.get("fork_id")
    batch = meta["cron"].get("batch") if isinstance(meta.get("cron"), dict) else None
    if not fid and not batch:
        return []
    head = f"{names.PRODUCT}: refused: no id given, and the newest rehearsal ({rid}) is "
    lines = []
    if fid:
        specs = []
        try:
            rec = fork.load_fork(fid)
            specs = [f"{fid}/{t['name']}" for t in rec.get("timelines") or []
                     if t.get("rid") and t.get("status") in ("finished", "capped")]
        except (OSError, ValueError):
            rec = {}
        if not specs:  # no fork record: name the timelines' rehearsals instead
            specs = [m["id"] for m in _metas() if m.get("fork_id") == fid]
        what = f"timeline {meta.get('timeline') or '?'} of fork {fid}"
        kept = [a["timeline"] for a in rec.get("applied") or [] if a.get("applied")]
        tail = f" ({kept[-1]} was applied before)" if kept else ""
        lines += report.wrap(head + what + ". Apply never picks a timeline for you. Name the one to keep"
                             + tail + ":", report._width(None))
    else:
        runs = sorted((m for m in _metas() if isinstance(m.get("cron"), dict) and m["cron"].get("batch") == batch),
                      key=lambda m: (m["cron"].get("run") or 0, m["id"]))
        specs = [m["id"] for m in runs] or [rid]
        c = meta["cron"]
        what = f"run {c.get('run') or '?'} of {c.get('of') or '?'} of cron batch {batch}"
        lines += report.wrap(head + what + ". Apply never picks a run for you. Name the one to apply:",
                             report._width(None))
    for s in specs:
        lines += _apply_line(s)
    lines.append("nothing was changed")
    return lines


def cmd_report(args):
    rid = state.resolve_id(args.id)
    if not rid and args.id in NO_ID:
        _none_yet("report")
        return 2
    if not rid:
        print(f"no rehearsal matches '{args.id}'", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_json(rid), indent=1, ensure_ascii=False, default=str))
    else:
        print(report.render_text(rid, show_diffs=not args.no_diffs, width=args.width, expand=args.expand))
    return 0


def cmd_apply(args):
    if state.any_armed():
        print("refusing to apply inside a rehearsal", file=sys.stderr)
        return apply_mod.EXIT_REFUSED
    rid = state.resolve_id(args.id)
    if not rid and args.id in NO_ID:
        _none_yet("apply")
        return apply_mod.EXIT_BAD_ID
    if not rid:
        print(f"no rehearsal matches '{args.id}'", file=sys.stderr)
        return apply_mod.EXIT_BAD_ID
    stop = not_yours_to_pick(rid) if args.id in NO_ID else []
    if stop:
        for ln in stop:
            print(ln, file=sys.stderr)
        return apply_mod.EXIT_REFUSED
    try:
        rc = apply_mod.apply(rid, only=_ids(args.only) or None, except_=_ids(args.except_) or None,
                             dry_run=args.dry_run, force=getattr(args, "force", False))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return apply_mod.EXIT_BAD_ID
    except RuntimeError as exc:  # still running, or another apply holds the lock
        print(f"{names.PRODUCT}: refused: {exc}", file=sys.stderr)
        return apply_mod.EXIT_REFUSED
    if args.json:
        print(json.dumps(rc, indent=1, ensure_ascii=False, default=str))
    else:
        print(report.render_receipt(rc))
    return apply_mod.exit_code(rc)
