"""`hermes clotho cron <job> [--runs N]`: rehearse an existing cron job's run.

Hermes does not let a plugin add a flag to `hermes cron`, so this is the plugin's own verb. Each run is
a separate `hermes clotho` child process (fail-closed the same way as `run`: if the plugin cannot
load, the child does not exist and nothing runs), with its own rehearsal id and ledger. For each run:

- the job is read from the job store without going through Hermes' job store code (which may repair
  and save the file); its schedule, run history, outputs and notepad are never written;
- the prompt is the one Hermes builds for a scheduled run of the job (cron instructions, run time,
  skills, notepad, upstream output, script output), with the notepad read from a read-only handle;
- a pre-run script runs inside the macOS sandbox with no network and no writes; a script that needed
  the network, wrote a file or could not be checked stops the run with the reason, before any model call;
- the agent runs as a one-shot with the toolsets a cron run gets; it is not recorded as a run of the job;
- the result is not delivered. The report says where it would have gone.

Reports are files (one per run plus a roll-up in ``<plugin data>/cron-runs/<batch>/``) and the usual
`hermes clotho report <id>`; never a chat message (post_message is refused inside cron runs).
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import io
import json
import os
import pathlib
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse

from . import names, report, state

BATCH_DIR = "cron-runs"
BATCH_PREFIX = "c-"
DEFAULT_TIMEOUT = 1200      # seconds per run (Hermes' cron inactivity limit is 600 s idle)
SCRIPT_TIMEOUT = 300        # seconds for a sandboxed pre-run script
CRON_DISABLED_ALWAYS = ("messaging", "clarify")


# ---------------------------------------------------------------- arguments
def add_parser(subs) -> None:
    p = subs.add_parser("cron", help="rehearse a cron job's run: its schedule and state are not "
                                     "touched and its result is not delivered")
    p.add_argument("job", nargs="?", default=None, help="job id or name, as `hermes cron list` shows it "
                                                        "(no job: list the jobs)")
    p.add_argument("--runs", type=int, default=1, help="how many runs to do, one after another (default 1)")
    p.add_argument("-m", "--model", default=None, help="model for the rehearsal (default: the job's)")
    p.add_argument("--provider", default=None, help="provider for the rehearsal (default: the job's)")
    p.add_argument("--workspace", default=None,
                   help="folder to rehearse in (default: the job's workdir, else the current folder)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per run before it is stopped")
    p.add_argument("--json", action="store_true", help="print the roll-up and the runs as JSON")
    p.add_argument("--no-diffs", action="store_true")
    p.add_argument("--expand", action="store_true", help="list every effect; similar ones are grouped by default")
    p.add_argument("--width", type=int, default=None,
                   help=f"columns per line (default {report.WIDTH}; 0 = this terminal's width)")
    p.add_argument("--child", default=None, help=argparse.SUPPRESS)   # internal: one run, in this process
    p.add_argument("--index", type=int, default=1, help=argparse.SUPPRESS)
    p.add_argument("--of", type=int, default=1, help=argparse.SUPPRESS)


def handle(args) -> int:
    try:
        if args.child:
            return child_run(args)
        if not args.job:
            print(render_jobs(load_jobs()))
            return 0
        return run_batch(args)
    except state.Refused as exc:
        print(f"{names.PRODUCT}: refused: {exc}", file=sys.stderr)
        return 3


# ---------------------------------------------------------------- the job store, read only
def hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def cron_dir(home=None) -> str:
    return os.path.join(home or hermes_home(), "cron")


def load_jobs(home=None) -> list:
    """Every job in ``<home>/cron/jobs.json``, read straight from the file (Hermes' loader may repair
    and save it; this never writes)."""
    p = os.path.join(cron_dir(home), "jobs.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise state.Refused(f"cannot read {p} ({type(exc).__name__}: {exc}); `hermes cron list` repairs it")
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
    if not isinstance(jobs, list):
        raise state.Refused(f"{p} holds no job list; `hermes cron list` repairs it")
    return [_normalize(j) for j in jobs if isinstance(j, dict) and j.get("id")]


def _normalize(job: dict) -> dict:
    try:
        from cron.jobs import _normalize_job_record  # pure: "Storage is untouched"
        return _normalize_job_record(dict(job))
    except Exception:
        return dict(job)


def find_job(ref: str, jobs: list) -> dict:
    """Exact id, then a unique id prefix, then the name (case-insensitive), as `hermes cron` does."""
    ref = (ref or "").strip()
    for j in jobs:
        if j["id"] == ref:
            return j
    hits = [j for j in jobs if ref and j["id"].startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    named = [j for j in jobs if str(j.get("name") or "").lower() == ref.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1 or len(hits) > 1:
        many = named if len(named) > 1 else hits
        raise state.Refused(f"'{ref}' matches {len(many)} jobs ({', '.join(j['id'] for j in many)}); use the id")
    raise state.Refused(f"no cron job matches '{ref}'; `hermes {names.CLI_NAME} cron` lists them")


def job_problems(job: dict, sandbox_ok=None) -> list:
    """Why a rehearsal of this job cannot be done safely (empty = it can)."""
    out = []
    if job.get("no_agent"):
        out.append("it is a script-only job (--no-agent): the script is the whole run and its output is "
                   "delivered as it is, so there is no agent run to rehearse")
    if job.get("monitor_script") or job.get("monitor_url"):
        out.append("it is a monitor job: each run first runs its source (a script or a URL fetch) and stores "
                   "the result, which a rehearsal cannot do without doing it for real")
    if job.get("base_url"):
        out.append("it pins its own base_url; a rehearsal would send the model calls somewhere else")
    if job.get("script") and sandbox_ok is not None and not sandbox_ok[0]:
        out.append(f"its pre-run script can only run in the sandbox, which is not available here ({sandbox_ok[1]})")
    if not str(job.get("prompt") or "").strip() and not job.get("skills") and not job.get("skill") \
            and not job.get("script"):
        out.append("it has nothing to run (no prompt, skill or script)")
    return out


def _sha_file(p) -> str | None:
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _ro_sqlite(path):
    """A read-only handle that creates nothing next to the file (no journal, no -shm)."""
    return sqlite3.connect(f"file:{urllib.parse.quote(path)}?immutable=1", uri=True)


def job_state(job_id: str, home=None) -> dict:
    """What a run of the job would change: its record (next run, last run, status, counters), its
    output documents and its rows in the run history. Read only."""
    rec = next((j for j in _raw_jobs(home) if j.get("id") == job_id), None)
    d = cron_dir(home)
    outs = os.path.join(d, "output", job_id)
    try:
        outputs = sorted(os.listdir(outs))
    except OSError:
        outputs = []
    runs = None
    db = os.path.join(d, "executions.db")
    if os.path.exists(db):
        try:
            with contextlib.closing(_ro_sqlite(db)) as c:
                runs = c.execute("SELECT COUNT(*) FROM executions WHERE job_id=?", (job_id,)).fetchone()[0]
        except sqlite3.Error:
            runs = None
    return {"record": hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest(),
            "next_run_at": (rec or {}).get("next_run_at"), "last_run_at": (rec or {}).get("last_run_at"),
            "outputs": outputs, "runs": runs}


def _raw_jobs(home=None) -> list:
    p = os.path.join(cron_dir(home), "jobs.json")
    try:
        with open(p, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def state_changes(before: dict, after: dict) -> list:
    out = []
    if before["record"] != after["record"]:
        moved = [k for k in ("next_run_at", "last_run_at") if before.get(k) != after.get(k)]
        out.append("its record changed" + (f" ({', '.join(moved)})" if moved else ""))
    if before["outputs"] != after["outputs"]:
        out.append(f"{len(set(after['outputs']) - set(before['outputs']))} new output document(s)")
    if before["runs"] != after["runs"]:
        out.append(f"run history {before['runs']} -> {after['runs']} rows")
    return out


def _notes_readonly(job_id: str, home=None) -> list:
    p = os.path.join(cron_dir(home), "notepad.db")
    if not os.path.exists(p):
        return []
    with contextlib.closing(_ro_sqlite(p)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT job_id, key, value, updated_at FROM cron_notepad WHERE job_id=? ORDER BY key",
                         (str(job_id),)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- one run (child process)
def _script_path(script: str):
    scripts = os.path.realpath(os.path.join(hermes_home(), "scripts"))
    raw = os.path.expanduser(str(script))
    p = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(scripts, raw))
    if not p.startswith(scripts + os.sep):
        return None, f"the script path resolves outside {scripts}: {script!r}"
    if not os.path.isfile(p):
        return None, f"script not found: {p}"
    return p, None


def run_script_sandboxed(r: state.Rehearsal, job: dict):
    """Run the job's pre-run script in the sandbox, in the rehearsal's copy of the workspace.
    Returns ``((success, output), None)`` the way Hermes' script runner reports it, or
    ``(None, reason)`` when the script cannot run without effects (the run then stops)."""
    from . import sandbox, snapshot
    ok, why = sandbox.available()
    if not ok:
        return None, f"the pre-run script needs the sandbox, which is not available ({why})"
    path, err = _script_path(job["script"])
    if path is None:
        return (False, err), None   # Hermes hands this error to the agent the same way
    from cron.scheduler_script import _script_argv
    argv, overlay, err = _script_argv(pathlib.Path(path), job.get("interpreter"))
    if argv is None:
        return (False, err), None
    exports = []
    for k, v in (overlay or {}).items():  # keep the sandbox's own PYTHONPATH entry first
        if k == "PYTHONPATH":
            exports.append(f'export PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}"{shlex.quote(v)}')
        else:
            exports.append(f"export {k}={shlex.quote(v)}")
    command = "\n".join(exports + [shlex.join(argv)])
    pre = snapshot.stat_manifest(r.work)
    res = sandbox.run(r.work, r.work, command, r.tmp, tag=f"{names.PLUGIN_NAME}-{r.id}-0", timeout=SCRIPT_TIMEOUT,
                      orig=r.workspace, home_overlay=r.home_overlay)
    post = snapshot.stat_manifest(r.work)
    reasons = sandbox.reasons_cant_preview(res, sandbox.static_scan(shlex.join(argv)))
    changed = sorted(set(pre) ^ set(post) | {k for k in pre if k in post and pre[k] != post[k]})
    if changed:
        reasons.append("writes " + ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else ""))
    if res.get("outside_changes") or res.get("outside_changes_discarded"):
        reasons.append("writes outside the workspace")
    r.write_meta(cron_script={"path": path, "exit_code": res.get("exit_code"), "reasons": reasons,
                              "stdout": (res.get("stdout") or "")[-4000:], "stderr": (res.get("stderr") or "")[-2000:]})
    if reasons:
        return None, (f"the pre-run script {os.path.basename(path)} cannot run without doing something for real: "
                      + "; ".join(dict.fromkeys(reasons)))
    stdout, stderr = (res.get("stdout") or "").strip(), (res.get("stderr") or "").strip()
    try:
        from agent.redact import redact_sensitive_text
        stdout, stderr = redact_sensitive_text(stdout), redact_sensitive_text(stderr)
    except Exception:
        stdout = stderr = "[REDACTED - redaction failed]"
    if res.get("exit_code") != 0:
        parts = [f"Script exited with code {res.get('exit_code')}"]
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        return (False, "\n".join(parts)), None
    return (True, stdout), None


def build_prompt(job: dict, prerun):
    """The prompt Hermes builds for a scheduled run of ``job`` (cron.scheduler._build_job_prompt), with
    the notepad read through a read-only handle (Hermes' reader opens the database for writing and
    creates it when it is missing). ``prerun`` is the script result; the script never runs here."""
    if job.get("script") and prerun is None:
        raise RuntimeError("the pre-run script must run in the sandbox first")
    import cron.scheduler  # noqa: F401  (loads the prompt module's late imports)
    from cron import notepad
    from cron.scheduler_prompt import _build_job_prompt
    orig = notepad.list_notes
    notepad.list_notes = _notes_readonly
    try:
        return _build_job_prompt(job, prerun_script=prerun)
    finally:
        notepad.list_notes = orig


def cron_toolsets(job: dict):
    """(toolsets, note): the toolsets a cron run of this job gets, as `hermes -z` takes them."""
    try:
        from hermes_cli.config import load_config
        from cron.scheduler import _resolve_cron_disabled_toolsets, _resolve_cron_enabled_toolsets
        cfg = load_config() or {}
        off = set(_resolve_cron_disabled_toolsets(cfg)) | set(CRON_DISABLED_ALWAYS)
        ts = [t for t in _resolve_cron_enabled_toolsets(job, cfg) if t not in off]
    except Exception as exc:
        raise state.Refused(f"cannot work out the toolsets a cron run of this job gets ({type(exc).__name__}: {exc})")
    if not ts:
        raise state.Refused("the job allows no toolsets; a one-shot run cannot be limited to none")
    from hermes_cli.oneshot import _validate_explicit_toolsets
    _, err = _validate_explicit_toolsets(ts)
    if err:
        raise state.Refused(f"the cron toolsets of this job are not accepted by a one-shot run: {err.strip()}")
    return ts


def child_run(args) -> int:
    """One run of the job as a rehearsal, in this process (started by run_batch)."""
    from . import capture, cli, sandbox
    out_path = os.path.join(args.child, f"run-{args.index}.json")
    outcome = {"index": args.index, "rid": None, "status": "refused", "error": None}

    def done(rc):
        with open(out_path + ".tmp", "w") as f:
            json.dump(outcome, f, indent=1, default=str)
        os.replace(out_path + ".tmp", out_path)
        return rc

    try:
        job = find_job(args.job, load_jobs())
        problems = job_problems(job, sandbox.available() if job.get("script") else None)
        if problems:
            raise state.Refused("; ".join(problems))
        model = args.model or job.get("model") or None
        provider = args.provider or job.get("provider") or None
        problems = cli.selfcheck(provider)
        if problems:
            raise state.Refused("nothing would be rehearsed safely: " + "; ".join(problems))
        toolsets = cron_toolsets(job)
        ws = resolve_workspace(job, args.workspace)
    except state.Refused as exc:
        outcome["error"] = str(exc)
        print(f"{names.PRODUCT}: refused: {exc}", file=sys.stderr)
        return done(3)

    r = state.Rehearsal(state.new_id(), ws, task=str(job.get("prompt") or job.get("name") or job["id"]),
                        mode="run", model=model, provider=provider)
    r.create()
    outcome["rid"] = r.id
    ok, why = sandbox.available()
    cron_meta = {"job_id": job["id"], "job_name": job.get("name"), "schedule": job.get("schedule_display"),
                 "run": args.index, "of": args.of, "batch": os.path.basename(args.child),
                 "deliver": job.get("deliver") or "local", "workdir": job.get("workdir"), "script": job.get("script"),
                 "skills": job.get("skills") or [], "toolsets": toolsets, "state": job.get("state")}
    r.write_meta(shell_preview="sandboxed clone" if ok else f"capture only ({why})", cron=cron_meta)
    state.arm_process(r)  # process-wide, never disarmed: late calls are blocked once the run is closed
    os.chdir(ws)
    os.environ["TERMINAL_CWD"] = ws
    print(f"{names.PRODUCT} {r.id}: cron job {job.get('name')} ({job['id']}) run {args.index}/{args.of}, "
          f"rehearsing in {ws} (nothing will happen for real)", file=sys.stderr)
    t0 = time.monotonic()

    def stop(status, why, rc):
        with r.lock:
            r.closed = True
        cli.finish(r, 0, os.path.join(r.dir, "usage.json"), time.monotonic() - t0)
        r.write_meta(status=status, refused=why if status == "refused" else None, error=None,
                     cron=dict(cron_meta, skipped=why if status == "skipped" else None))
        outcome.update(status=status, error=why)
        print(f"{names.PRODUCT}: {status}: {why}", file=sys.stderr)
        return done(rc)

    prerun = None
    if job.get("script"):
        prerun, why = run_script_sandboxed(r, job)
        if prerun is None:
            return stop("refused", why, 3)
        from cron.scheduler_prompt import _parse_wake_gate
        if prerun[0] and not _parse_wake_gate(prerun[1]):
            return stop("skipped", "the pre-run script returned wakeAgent=false: Hermes would not start the agent", 0)
    try:
        with capture.usage_kept(r):  # loading the job's skills must not move Hermes' usage counters
            prompt = build_prompt(job, prerun)
    except Exception as exc:
        if type(exc).__name__ == "CronPromptInjectionBlocked":
            return stop("refused", f"Hermes' cron prompt scanner blocks this job's prompt: {exc}", 3)
        return stop("refused", f"cannot build the job's prompt ({type(exc).__name__}: {exc})", 3)
    if prompt is None:
        return stop("skipped", "the pre-run script printed nothing: Hermes would not start the agent", 0)
    with open(os.path.join(r.dir, "cron_prompt.txt"), "w") as f:
        f.write(prompt)

    usage = os.path.join(r.dir, "usage.json")
    buf = io.StringIO()
    from hermes_cli.oneshot import run_oneshot
    rc, err = 1, None
    try:
        with contextlib.redirect_stdout(buf):
            rc = run_oneshot(prompt, model=model, provider=provider, usage_file=usage, toolsets=toolsets)
    except BaseException as exc:  # still finalize what was captured
        err = f"{type(exc).__name__}: {exc}"
    text = buf.getvalue()
    cli.finish(r, rc, usage, time.monotonic() - t0, agent_text=text, error=err)
    silent = False
    try:
        from cron.scheduler import _is_cron_silence_response
        silent = bool(_is_cron_silence_response(text or ""))
    except Exception:
        pass
    r.write_meta(cron=dict(cron_meta, delivery={"target": cron_meta["deliver"], "held": True,
                                                "would_deliver": bool(text.strip()) and not silent,
                                                "silent": silent}))
    outcome.update(status="finished" if rc == 0 and not err else "failed", error=err,
                   exit_code=rc)
    return done(0 if rc == 0 and not err else (rc or 1))


def resolve_workspace(job: dict, given=None) -> str:
    if given:
        ws = os.path.realpath(given)
    elif job.get("workdir") and os.path.isdir(job["workdir"]):
        ws = os.path.realpath(job["workdir"])
    else:
        ws = os.path.realpath(os.getcwd())
    if not os.path.isdir(ws):
        raise state.Refused(f"workspace {ws} is not a folder")
    if ws in ("/", os.path.realpath(os.path.expanduser("~"))):
        why = "the job has no workdir, so this would rehearse in " if not given and not job.get("workdir") else ""
        raise state.Refused(f"{why}{ws}; run from a project folder or pass --workspace")
    return ws


# ---------------------------------------------------------------- the batch (parent process)
def _hermes_src() -> str:
    import hermes_cli
    return os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__)))


_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['hermes'] + sys.argv[2:]; "
         "from hermes_cli.main import main; main()")


def _child_argv(job_id, batch, k, n, ws, model, provider) -> list:
    argv = [sys.executable, "-B"] + (["-I"] if sys.flags.isolated else []) + \
           ["-c", _BOOT, _hermes_src(), names.CLI_NAME, "cron", job_id, "--child", batch, "--index", str(k),
            "--of", str(n), "--workspace", ws]
    if model:
        argv += ["-m", model]
    if provider:
        argv += ["--provider", provider]
    return argv


def _claim_batch() -> str:
    root = os.path.join(state.data_root(), BATCH_DIR)
    os.makedirs(root, exist_ok=True)
    for _ in range(64):
        d = os.path.join(root, BATCH_PREFIX + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2))
        try:
            os.mkdir(d)
            return d
        except FileExistsError:
            continue
    raise state.Refused(f"could not claim a folder in {root}")


def _run_child(argv, batch, k, timeout) -> dict:
    log = os.path.join(batch, f"run-{k}.log")
    pidf = os.path.join(batch, f"run-{k}.pid")
    t0 = time.monotonic()
    with open(log, "w") as lf:
        p = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        with open(pidf, "w") as f:
            f.write(str(p.pid))
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            _stop(p, pidf)
            if not timed_out:
                raise
    try:
        with open(os.path.join(batch, f"run-{k}.json")) as f:
            out = json.load(f)
    except (OSError, ValueError):
        tail = ""
        try:
            with open(log, errors="replace") as f:
                tail = f.read()[-600:].strip()
        except OSError:
            pass
        out = {"index": k, "rid": None, "status": "failed",
               "error": (f"stopped after {timeout} s" if timed_out else f"the run exited {p.returncode}")
               + (f": {tail}" if tail else "")}
    if timed_out:
        out.update(status="failed", error=f"stopped after {timeout} s")
    out.update(index=k, exit=p.returncode, seconds=round(time.monotonic() - t0, 1), log=log)
    return out


def _stop(p, pidf):
    """Stop a run by the pid it was started with (its own session, sandboxed children too)."""
    try:
        with open(pidf) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    if pid != p.pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_batch(args) -> int:
    from . import cli, sandbox
    if args.runs < 1:
        raise state.Refused("--runs must be 1 or more")
    job = find_job(args.job, load_jobs())
    problems = job_problems(job, sandbox.available() if job.get("script") else None)
    if problems:
        raise state.Refused(f"cron job {job.get('name')} ({job['id']}) cannot be rehearsed: " + "; ".join(problems))
    model = args.model or job.get("model") or None
    provider = args.provider or job.get("provider") or None
    probs = cli.selfcheck(provider)
    if probs:
        raise state.Refused("nothing would be rehearsed safely: " + "; ".join(probs))
    ws = resolve_workspace(job, args.workspace)
    batch = _claim_batch()
    before = job_state(job["id"])
    color = report._use_color() and not args.json
    runs = []
    for k in range(1, args.runs + 1):
        print(f"{names.PRODUCT}: cron job {job.get('name')} ({job['id']}) run {k}/{args.runs} ...", file=sys.stderr)
        o = _run_child(_child_argv(job["id"], batch, k, args.runs, ws, args.model, args.provider), batch, k,
                       args.timeout)
        o["rows"] = _rows(o.get("rid"))
        runs.append(o)
        text = render_run(o, job, show_diffs=not args.no_diffs, width=args.width, expand=args.expand, color=False)
        with open(os.path.join(batch, f"run-{k}.report.txt"), "w") as f:
            f.write(text + "\n")
        if not args.json:
            print(render_run(o, job, show_diffs=not args.no_diffs, width=args.width, expand=args.expand,
                             color=color) if color else text)
            print()
        if o["status"] == "refused":
            break  # the same reason would stop every later run
    after = job_state(job["id"])
    roll = rollup(runs, planned=args.runs)
    roll.update(job_id=job["id"], job_name=job.get("name"), batch=batch, state_changes=state_changes(before, after),
                state_before=before, state_after=after, deliver=job.get("deliver") or "local")
    text = render_rollup(roll, width=args.width)
    with open(os.path.join(batch, "rollup.txt"), "w") as f:
        f.write(text + "\n")
    with open(os.path.join(batch, "batch.json"), "w") as f:
        json.dump(dict(roll, runs=[{k: v for k, v in o.items() if k != "rows"} for o in runs]), f, indent=1,
                  default=str)
    if args.json:
        print(json.dumps(dict(roll, runs=[{k: v for k, v in o.items() if k != "rows"} for o in runs]), indent=1,
                         default=str))
    else:
        print(text)
    if any(o["status"] == "refused" for o in runs):
        return 3
    return 0 if all(o["status"] in ("finished", "skipped") for o in runs) else 1


def _rows(rid) -> list:
    if not rid:
        return []
    try:
        from .ledger import load
        return load(os.path.join(state.rehearsal_dir(rid), "ledger.jsonl"))
    except Exception:
        return []


# ---------------------------------------------------------------- reports
def _meta(rid) -> dict:
    try:
        with open(os.path.join(state.rehearsal_dir(rid), "meta.json")) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def render_run(o: dict, job: dict, show_diffs=True, width=None, expand=False, color=None) -> str:
    w = report._width(width)
    head = f"run {o['index']} · cron job {job.get('name')} ({job['id']})"
    lines = [report.fit(head, w)]
    if o.get("rid") and o["status"] in ("finished", "failed"):
        meta = _meta(o["rid"])
        text = (meta.get("agent_final_text") or "").strip()
        delivery = (meta.get("cron") or {}).get("delivery") or {}
        lines.append(report.render_text(o["rid"], show_diffs=show_diffs, color=color, width=width, expand=expand))
        if delivery.get("silent"):
            lines += report.wrap(f"delivery: none (the agent answered [SILENT]; Hermes would not deliver)", w)
        elif delivery.get("would_deliver"):
            lines += report.wrap(f"delivery held: the final answer would go to {delivery.get('target')}", w)
            for t in text.splitlines()[:report.MAX_TEXT_LINES]:
                lines += report.wrap("| " + t, w)
        else:
            lines += report.wrap("delivery: none (no final answer)", w)
        if o["status"] == "failed":
            lines += report.wrap(f"the run failed: {o.get('error') or 'exit ' + str(o.get('exit'))}", w)
    else:
        lines += report.wrap(f"{o['status']}: {o.get('error') or 'no reason given'}", w)
        if o.get("rid"):
            lines += report.wrap(f"rehearsal {o['rid']}", w)
    return "\n".join(lines)


def _sig(rows) -> collections.Counter:
    """Effects as comparable (key, payload) pairs, counted the way the report counts them."""
    c = collections.Counter()
    for r in rows:
        if r.get("vcs_internal") or (r.get("children") and r.get("class") == "command"):
            continue
        d = report.describe(r)
        c[(d["kind"], d["key"], d.get("payload") or "")] += 1
    return c


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def diff_words(base: collections.Counter, other: collections.Counter) -> str:
    """"+1 message, -2 deletes, 1 web request with other content": how ``other`` differs from ``base``."""
    added, removed = other - base, base - other
    changed = collections.Counter()
    for (kind, key, pay), n in list(added.items()):
        for (k2, key2, pay2), m in list(removed.items()):
            if key2 == key and m and n:
                take = min(n, m)
                changed[kind] += take
                added[(kind, key, pay)] -= take
                removed[(k2, key2, pay2)] -= take
                n -= take
    plus, minus = collections.Counter(), collections.Counter()
    for (kind, _, _), n in (+added).items():
        plus[kind] += n
    for (kind, _, _), n in (+removed).items():
        minus[kind] += n
    parts = [f"+{_plural(n, k)}" for k, n in sorted(plus.items())]
    parts += [f"-{_plural(n, k)}" for k, n in sorted(minus.items())]
    parts += [f"{_plural(n, k)} with other content" for k, n in sorted(changed.items())]
    return ", ".join(parts) or "same effects"


def rollup(runs: list, planned=None) -> dict:
    """One line for N runs ("3 runs: 2 identical, 1 differs: +1 message") plus one line per run.
    The baseline is the most common set of effects (the earliest run on a tie)."""
    done = [o for o in runs if o.get("status") == "finished"]
    sigs = {o["index"]: _sig(o.get("rows") or []) for o in done}
    groups: list = []   # [(signature, [indexes])]
    for o in done:
        for g in groups:
            if g[0] == sigs[o["index"]]:
                g[1].append(o["index"])
                break
        else:
            groups.append((sigs[o["index"]], [o["index"]]))
    base = max(groups, key=lambda g: (len(g[1]), -g[1][0])) if groups else None
    per = []
    for o in runs:
        k = o["index"]
        if o.get("status") != "finished":
            per.append({"run": k, "rid": o.get("rid"), "status": o.get("status"), "text": o.get("error") or o.get("status")})
            continue
        n_eff = sum(sigs[k].values())
        if k == base[1][0]:
            txt = f"{_plural(n_eff, 'effect')} (baseline)"
        elif k in base[1]:
            txt = f"same as run {base[1][0]}"
        else:
            txt = diff_words(base[0], sigs[k])
        per.append({"run": k, "rid": o.get("rid"), "status": "finished", "text": txt, "effects": n_eff,
                    "identical": k in base[1]})
    n = len(runs)
    head = f"{_plural(max(n, planned or 0), 'run')}:"
    bits = []
    if done:
        same = len(base[1])
        bits.append(f"{same} identical")
        differ = [p for p in per if p["status"] == "finished" and not p["identical"]]
        if differ:
            first = differ[0]["text"]
            bits.append(f"{len(differ)} {'differs' if len(differ) == 1 else 'differ'}: {first}"
                        + (" (and more, below)" if len({p['text'] for p in differ}) > 1 else ""))
    for st in ("failed", "refused", "skipped"):
        m = sum(1 for o in runs if o.get("status") == st)
        if m:
            bits.append(f"{m} {st}")
    if planned and planned > n:
        bits.append(f"{planned - n} not started")
    line = head + " " + ", ".join(bits) if bits else head + " nothing ran"
    return {"line": line, "runs_planned": planned or n, "runs_done": n, "per_run": per,
            "baseline": base[1][0] if base else None}


def _shown_folder(path: str, home=None) -> str:
    """A folder as printed: under the Hermes home as ``<Hermes home>/...``, under the user's home as
    ``~/...``, never with the absolute path (it carries the user name and the home's location)."""
    real = os.path.realpath(path)
    for root, label in ((home or hermes_home(), "<Hermes home>"), (os.path.expanduser("~"), "~")):
        root = os.path.realpath(root)
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            rel = os.path.relpath(real, root)
            return label + "/" + ("" if rel == "." else rel + "/")
    return path.rstrip(os.sep) + "/"


def render_rollup(roll: dict, width=None, home=None) -> str:
    w = report._width(width)
    lines = [report.fit(f"{names.PRODUCT} cron · {roll.get('job_name')} ({roll.get('job_id')})", w)]
    lines += report.wrap(roll["line"], w)
    for p in roll["per_run"]:
        lines += report.wrap(f"run {p['run']}  {p.get('rid') or '-'}  {p['text']}", w)
    if any(p["status"] == "finished" for p in roll["per_run"]):
        lines += report.wrap(f"delivery: held on every run (the job delivers to {roll.get('deliver')})", w)
    else:
        lines += report.wrap("delivery: none (no run reached the agent)", w)
    ch = roll.get("state_changes")
    if ch:
        lines += report.wrap("job state: CHANGED during the rehearsal: " + "; ".join(ch)
                             + " (the scheduler may have run the job meanwhile)", w)
    else:
        nxt = (roll.get("state_after") or {}).get("next_run_at")
        lines += report.wrap("job state: untouched (schedule, history, outputs)"
                             + (f"; next run {nxt}" if nxt else ""), w)
    batch = roll.get("batch")
    if batch:
        lines += report.wrap(f"reports: {os.path.basename(batch.rstrip(os.sep))}", w)
        lines += ["  " + s for s in report.wrap(f"in {_shown_folder(os.path.dirname(batch.rstrip(os.sep)), home)}",
                                                w - 2)]
    base = next((p["rid"] for p in roll["per_run"] if p["run"] == roll.get("baseline") and p.get("rid")), None)
    if base:
        lines += report.wrap(f"review one: hermes {names.CLI_NAME} report {base}", w)
        lines += report.wrap(f"apply one:  hermes {names.CLI_NAME} apply {base}", w)
    return "\n".join(lines)


def render_jobs(jobs: list, width=None) -> str:
    w = report._width(width)
    if not jobs:
        return "no cron jobs in this Hermes home (`hermes cron create` makes one)"
    lines = []
    for j in jobs:
        extra = [x for x in (j.get("schedule_display"), j.get("state"),
                             "script" if j.get("script") else None,
                             f"delivers to {j.get('deliver') or 'local'}") if x]
        lines += report.wrap(f"{j['id']}  {j.get('name')}  " + " · ".join(extra), w)
    lines += report.wrap(f"rehearse one: hermes {names.CLI_NAME} cron <id or name> --runs N", w)
    return "\n".join(lines)
