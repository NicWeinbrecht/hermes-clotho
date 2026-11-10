"""Forks: one task, several rehearsals at once (a model or an instruction per timeline), compared side by
side, and one of them applied.

Because a rehearsal has no side effects, timelines cannot step on each other: each is its own
`hermes clotho run` process (Hermes arms middleware per process), with its own rehearsal id, clone,
overlay and ledger. The parent only starts them, waits, and writes the fork record:

    <plugin data>/forks/<fork id>/fork.json        timelines, rehearsal ids, status, wall times
    <plugin data>/forks/<fork id>/<timeline>.*     the child's stdout (report JSON), stderr, pid, model-call log

Spend cap: every child gets a per-timeline cap on model calls. Over the cap, the model call is answered
with a synthetic "stop now" reply and the provider is never called (raising inside llm_execution would fail
open into the provider, so the cap never raises).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time

from . import names, state

FORK_PREFIX = names.FORK_PREFIX       # fork ids look like f-20261110-101500-ab12
FORKS_DIR = names.FORKS_DIR           # folder under the plugin data root
FORK_SCHEMA = f"{names.PLUGIN_NAME}.fork.v1"
CLAUDE_PLAN_PROVIDER = "claude-subscription-directsdk-experimental"
MAX_PLAN_TIMELINES = 1             # at most one timeline on the Claude-subscription provider per fork
DEFAULT_MAX_CALLS = 25             # model calls per timeline
DEFAULT_MAX_PLAN_CALLS = 10        # model calls for a Claude-subscription timeline (its plan is shared)
DEFAULT_TIMEOUT = 900              # seconds per timeline before it is stopped by its pid

ENV_FORK_ID = names.env("FORK_ID")
ENV_TIMELINE = names.env("TIMELINE")
ENV_MAX_CALLS = names.env("MAX_MODEL_CALLS")
ENV_CALLS_LOG = names.env("MODEL_CALLS_LOG")
TEST_COLLIDE_ENV = names.env("TEST_COLLIDE")  # test seam: these timelines fail as collided in the first batch

ALIASES = {
    "flash": "deepseek/deepseek-v4-flash@openrouter",
    "pro": "deepseek/deepseek-v4-pro@openrouter",
    "kimi": "moonshotai/kimi-k2.6@openrouter",
    "claude": f"sonnet@{CLAUDE_PLAN_PROVIDER}",
}
AGGREGATOR = "openrouter"          # where a vendor/model id goes when Hermes' own pick has no credentials here

# A model that cannot drive the tool loop is dropped from the fork (never faked). These are the provider
# answers that say so; the first was seen for nousresearch/hermes-4-405b on OpenRouter.
_CANNOT_DRIVE = re.compile(r"no endpoints found that support tool use|does not support (tools|tool use|function"
                           r" calling)|tool[ _-]?(use|calls?|calling) (is|are) not supported|"
                           r"tools? (is|are) not supported", re.I)
# Errors that mean two timelines got in each other's way (shared state, ports, rate limits), not that the
# task failed. Those timelines are run again one after another.
_COLLIDED = re.compile(r"database is locked|resource temporarily unavailable|address already in use|"
                       r"rate[ _-]?limit|too many requests|\b429\b|FileExistsError|could not claim a fresh", re.I)
_NAME_OK = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------- ids and folders
def forks_root() -> str:
    p = os.path.join(state.data_root(), FORKS_DIR)
    os.makedirs(p, exist_ok=True)
    return p


def fork_dir(fid: str) -> str:
    return os.path.join(forks_root(), fid)


def _new_fork_id() -> str:
    return FORK_PREFIX + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def _claim_fork_dir() -> tuple[str, str]:
    root = forks_root()
    for _ in range(64):
        fid = _new_fork_id()
        try:
            os.mkdir(os.path.join(root, fid))
            return fid, os.path.join(root, fid)
        except FileExistsError:
            continue
    raise state.Refused(f"could not claim a fresh fork folder in {root}")


def resolve_fork(fid_or_prefix: str | None) -> str | None:
    """Full fork id from an id, a unique prefix or suffix, or "last"/None (most recent)."""
    root = forks_root()
    ids = sorted(d for d in os.listdir(root) if d.startswith(FORK_PREFIX)
                 and os.path.exists(os.path.join(root, d, "fork.json")))
    if not ids:
        return None
    if not fid_or_prefix or fid_or_prefix in ("last", "latest"):
        return ids[-1]
    if fid_or_prefix in ids:
        return fid_or_prefix
    hits = [i for i in ids if i.startswith(fid_or_prefix) or i.endswith(fid_or_prefix)]
    return hits[0] if len(hits) == 1 else None


def load_fork(fid: str) -> dict:
    with open(os.path.join(fork_dir(fid), "fork.json")) as f:
        return json.load(f)


def _save_fork(fdir: str, rec: dict) -> None:
    tmp = os.path.join(fdir, "fork.json.tmp")
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, os.path.join(fdir, "fork.json"))


def resolve_timeline(spec: str):
    """``<fork id>/<timeline>`` -> (fork id, timeline record) or raises ValueError naming what is missing."""
    fpart, _, tpart = (spec or "").partition("/")
    fid = resolve_fork(fpart or "last")
    if not fid:
        raise ValueError(f"no fork matches '{fpart}'")
    rec = load_fork(fid)
    tls = rec.get("timelines") or []
    hit = [t for t in tls if t["name"] == tpart] or [t for t in tls if t["name"].startswith(tpart)] if tpart else []
    if len(hit) != 1:
        have = ", ".join(t["name"] for t in tls)
        raise ValueError(f"fork {fid} has no single timeline '{tpart}' (timelines: {have})")
    return fid, hit[0]


# ---------------------------------------------------------------- timeline specs
def _short(model: str, provider: str | None) -> str:
    base = (model or "default").split("/")[-1]
    if provider == CLAUDE_PLAN_PROVIDER and not base.startswith("claude"):
        base = "claude-" + base
    return _NAME_OK.sub("-", base).strip("-") or "timeline"


def parse_model(spec: str) -> dict:
    """``[name=]model[@provider]`` (or an alias: flash, pro, kimi, claude)."""
    spec = spec.strip()
    name = None
    if "=" in spec:
        name, spec = (x.strip() for x in spec.split("=", 1))
    spec = ALIASES.get(spec, spec)
    model, _, provider = spec.partition("@")
    model, provider = model.strip() or None, (provider.strip() or None)
    return {"name": _NAME_OK.sub("-", name).strip("-") if name else _short(model, provider),
            "model": model, "provider": provider, "instructions": None, "prompt": None}


def _read_variants(path: str) -> dict:
    with open(path) as f:
        text = f.read()
    try:
        from ruamel.yaml import YAML
        data = YAML(typ="safe").load(text)
    except ImportError:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with a 'variants' list")
    return data


def parse_variants(path: str, default_model=None, default_provider=None) -> list:
    """Same model, different instructions. File format (YAML or JSON):

        model: deepseek/deepseek-v4-flash       # optional default for every variant
        variants:
          - name: tidy
            instructions: Tidy it up.          # added after the task
          - name: space
            prompt: My disk is nearly full ... # replaces the task
            model: deepseek/deepseek-v4-pro    # optional, per variant
    """
    data = _read_variants(path)
    raw = data.get("variants")
    if isinstance(raw, dict):
        raw = [{"name": k, "instructions": v} for k, v in raw.items()]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: 'variants' must be a non-empty list")
    dm = data.get("model") or default_model
    dp = data.get("provider") or default_provider
    if dm and "@" in str(dm) and not dp:
        dm, dp = str(dm).split("@", 1)
    out = []
    for i, v in enumerate(raw):
        if not isinstance(v, dict):
            v = {"instructions": str(v)}
        model = v.get("model") or dm
        provider = v.get("provider") or dp
        if model and "@" in str(model) and not v.get("provider"):
            model, provider = str(model).split("@", 1)
        name = v.get("name") or f"variant-{i + 1}"
        out.append({"name": _NAME_OK.sub("-", str(name)).strip("-") or f"variant-{i + 1}",
                    "model": ALIASES.get(model, model) if model else None, "provider": provider,
                    "instructions": v.get("instructions"), "prompt": v.get("prompt")})
    for t in out:
        if t["model"] and "@" in t["model"]:
            t["model"], t["provider"] = t["model"].split("@", 1)
    return out


def _dedupe(tls: list) -> list:
    seen = {}
    for t in tls:
        n = t["name"]
        if n in seen:
            seen[n] += 1
            t["name"] = f"{n}-{seen[n]}"
        else:
            seen[n] = 1
    return tls


def prompt_for(task: str | None, t: dict) -> str:
    if t.get("prompt"):
        return str(t["prompt"])
    if t.get("instructions"):
        if not task:
            raise ValueError(f"timeline {t['name']}: 'instructions' are added to a task, but no task was given")
        return f"{task}\n\n{t['instructions']}"
    if not task:
        raise ValueError(f"timeline {t['name']}: no task and no prompt")
    return task


def is_plan_provider(provider) -> bool:
    return bool(provider) and "claude-subscription" in str(provider).lower()


# ---------------------------------------------------------------- which provider serves a bare model id
def _hermes_choice(model: str):
    """(provider, model) Hermes itself would pick for `-m model` with no --provider in this home (its auto
    rule: the configured default's catalog, static catalogs, then OpenRouter's), or (None, model)."""
    from hermes_cli.config import load_config
    from hermes_cli.oneshot import _resolve_model_and_provider
    c = _resolve_model_and_provider(load_config() or {}, model, None)
    return c.provider, c.model or model


def _has_credentials(provider: str) -> bool:
    from hermes_cli.models_detect import provider_has_credentials
    return bool(provider_has_credentials(provider))


def _home_default():
    """The home's own default provider (config model.provider, then HERMES_INFERENCE_PROVIDER), or None."""
    from . import cli
    name, _route = cli.effective_provider(None)
    return None if name in ("", "auto") else name


def pin_provider(t: dict, choice=None, has_credentials=None, home_default=None) -> dict:
    """Give a timeline with a model and no provider the provider it will really use, decided here once, so
    every child is started with an explicit --provider and nothing depends on a guess inside the child.
    In order: Hermes' own pick for this home when it holds credentials for it; else the home's default;
    else OpenRouter for a vendor/model id when this home has an OpenRouter key; else Hermes decides (and
    names the provider it lacks credentials for). ``t["provider_from"]`` says which rule applied."""
    if t.get("provider"):
        t.setdefault("provider_from", "given")
        return t
    if not t.get("model"):
        t["provider_from"] = "home default model"
        return t
    choice = choice or _hermes_choice
    has_credentials = has_credentials or _has_credentials
    home_default = _home_default if home_default is None else home_default
    try:
        picked, model = choice(t["model"])
    except Exception as exc:  # Hermes' resolver moved or failed: leave it to the child, say so
        t["provider_from"] = f"Hermes in the timeline (could not ask it here: {type(exc).__name__})"
        return t
    def creds(p):
        try:
            return has_credentials(p)
        except Exception:
            return False
    if picked and creds(picked):
        t.update(provider=picked, model=model or t["model"], provider_from="Hermes' choice for this home")
        return t
    default = home_default() if callable(home_default) else home_default
    if not picked and default:
        t.update(provider=default, provider_from="the home's default provider")
        return t
    if "/" in t["model"] and creds(AGGREGATOR):
        why = f"Hermes picked {picked}, which has no credentials here" if picked else "no provider configured"
        t.update(provider=AGGREGATOR, provider_from=f"{AGGREGATOR} ({why})")
        return t
    t["provider_from"] = "Hermes in the timeline" + (f" (it picks {picked}, which has no credentials here)"
                                                     if picked else "")
    return t


# ---------------------------------------------------------------- the model-call cap (runs in each child)
_cap_lock = threading.Lock()
_cap_state = {"n": 0, "cap": 0, "log": None, "installed": False}


def _synthetic_stop(text: str, model):
    from openai.types.chat import ChatCompletion
    return ChatCompletion.model_validate({
        "id": f"{names.PLUGIN_NAME}-cap-stop", "object": "chat.completion", "created": int(time.time()),
        "model": str(model or "capped"),
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


def _log_call(rec: dict) -> None:
    p = _cap_state["log"]
    if not p:
        return
    try:
        with open(p, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def cap_middleware(**kw):
    """llm_execution middleware, registered first by the plugin (__init__.py); armed only in a timeline
    process (arm_cap). Counts model calls and answers every call over the cap itself. It never raises
    (Hermes would skip a raising middleware and call the provider). Not armed: passes straight through."""
    next_call, request = kw.get("next_call"), kw.get("request")
    if not _cap_state["installed"]:
        return next_call(request)
    with _cap_lock:
        _cap_state["n"] += 1
        k = _cap_state["n"]
    cap = _cap_state["cap"]
    capped = bool(cap) and k > cap
    _log_call({"k": k, "t": round(time.time(), 3), "model": kw.get("model"), "provider": kw.get("provider"),
               "api_mode": kw.get("api_mode"), "capped": capped})
    if not capped:
        return next_call(request)
    try:
        return _synthetic_stop(f"{names.RESULT_TAG} This timeline reached its cap of {cap} model calls. "
                               "Stop now: no more tool calls. The rehearsal ends here.", kw.get("model"))
    except BaseException as exc:  # no reply could be built: stop the process rather than call the provider
        try:
            sys.__stderr__.write(f"{names.PRODUCT}: model-call cap reached and no stop reply could be built "
                                 f"({type(exc).__name__}: {exc}); stopping this timeline.\n")
            sys.__stderr__.flush()
        finally:
            os._exit(5)


def arm_cap() -> None:
    """Arm the cap in this timeline process, from its environment (idempotent). Called by `hermes clotho
    run` right before the model starts. Raises when the cap middleware is not in this process's chain."""
    if _cap_state["installed"]:
        return
    if os.environ.get(names.env("TEST_FAULT")) == "cap":  # test seam (as capture.FAULT_ENV)
        raise RuntimeError("injected fault (cap)")
    from hermes_cli.plugins import _delivery_manager  # read only, as cli.selfcheck reads it
    if cap_middleware not in list(_delivery_manager()._middleware.get("llm_execution", [])):
        raise RuntimeError("the model-call cap is not registered in this process")
    _cap_state["cap"] = int(os.environ.get(ENV_MAX_CALLS) or 0)
    _cap_state["log"] = os.environ.get(ENV_CALLS_LOG) or None
    _cap_state["installed"] = True


# The child process: the stock `hermes clotho run` command, started with the same interpreter. The cap is
# the plugin's own registered middleware; `hermes clotho run` arms it from the environment (ENV_FORK_ID set)
# before the model starts, and stops before any model call if it cannot.
_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['hermes'] + sys.argv[3:]; "
         "from hermes_cli.main import main; main()")


def _hermes_src() -> str:
    import hermes_cli
    return os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__)))


def _child_argv(t: dict, prompt: str, ws: str, toolsets, boot: str | None = None) -> list:
    """The timeline's command line. ``boot`` replaces _BOOT; it gets the same argv (Hermes' source folder,
    this file's path, then the `hermes` arguments) and must start the stock Hermes CLI, as _BOOT does; it must
    not wrap or rebind Hermes code (the cap is armed by `hermes clotho run` itself)."""
    argv = [sys.executable, "-B"] + (["-I"] if sys.flags.isolated else []) + \
           ["-c", boot or _BOOT, _hermes_src(), os.path.abspath(__file__), names.CLI_NAME, "run", prompt,
            "--workspace", ws, "--json", "--no-diffs"]
    if t.get("model"):
        argv += ["-m", t["model"]]
    if t.get("provider"):
        argv += ["--provider", t["provider"]]
    if toolsets:
        argv += ["--toolsets", toolsets]
    return argv


# ---------------------------------------------------------------- running the timelines
class _Child:
    def __init__(self, t, fdir, fid, argv, cap):
        self.t, self.fdir, self.argv = t, fdir, argv
        base = os.path.join(fdir, t["name"])
        self.out_path, self.err_path = base + ".out.json", base + ".err.txt"
        self.pid_path, self.calls_path = base + ".pid", base + ".calls.jsonl"
        self.env = dict(os.environ)
        self.env.update({ENV_FORK_ID: fid, ENV_TIMELINE: t["name"], ENV_MAX_CALLS: str(cap),
                         ENV_CALLS_LOG: self.calls_path})
        self.proc = None
        self.t0 = self.t1 = None
        self.timed_out = False

    def start(self, cwd):
        self.t0 = time.monotonic()
        self._out = open(self.out_path, "w")
        self._err = open(self.err_path, "w")
        self.proc = subprocess.Popen(self.argv, cwd=cwd, env=self.env, stdout=self._out, stderr=self._err,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        with open(self.pid_path, "w") as f:
            f.write(str(self.proc.pid))

    def stop(self):
        """Stop this timeline by the pid it was started with (its own session: sandboxed children too)."""
        try:
            with open(self.pid_path) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return
        if pid != self.proc.pid:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                return
            try:
                self.proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    def wait(self, deadline):
        try:
            self.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.stop()
        self.t1 = time.monotonic()
        self._out.close()
        self._err.close()


def _read(p, limit=200_000) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _calls(p) -> list:
    out = []
    for line in _read(p, 5_000_000).splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _outcome(c: _Child) -> dict:
    """What happened to one timeline, from its exit code, report JSON, meta.json and stderr."""
    t = c.t
    out, err = _read(c.out_path), _read(c.err_path)
    rid = None
    try:
        rid = json.loads(out)["rehearsal"]["id"]
    except (ValueError, KeyError, TypeError):
        m = re.search(re.escape(names.RID_PREFIX) + r"\d{8}-\d{6}-[0-9a-f]{4}", err)
        rid = m.group(0) if m else None
    meta = {}
    if rid:
        try:
            with open(os.path.join(state.rehearsal_dir(rid), "meta.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
    calls = _calls(c.calls_path)
    real = [x for x in calls if not x.get("capped")]
    capped = [x for x in calls if x.get("capped")]
    models = sorted({str(x.get("model")) for x in real if x.get("model")})
    text = "\n".join(str(meta.get(k) or "") for k in ("error", "agent_final_text", "refused")) + "\n" + err
    rc = c.proc.returncode
    res = {"rid": rid, "exit_code": rc, "seconds": round((c.t1 or c.t0) - c.t0, 2), "model_calls": len(real),
           "capped_calls": len(capped), "cap": int(c.env[ENV_MAX_CALLS]), "models_seen": models,
           "stdout": os.path.basename(c.out_path), "stderr": os.path.basename(c.err_path)}
    if t.get("model") and models and any(m != t["model"] and not m.endswith("/" + t["model"]) for m in models):
        res["model_switched"] = models  # a fallback chain answered some calls with another model
    if c.timed_out:
        res.update(status="timed out", reason=f"stopped by its pid after {res['seconds']:.0f} s")
    elif _CANNOT_DRIVE.search(text):
        line = next((ln for ln in text.splitlines() if _CANNOT_DRIVE.search(ln)), "")
        res.update(status="dropped", reason="cannot drive the tool loop: " + line.strip()[:300])
    elif rc == 0 and rid and meta.get("status") == "finished":
        res.update(status="capped" if capped else "finished")
        if capped:
            res["reason"] = f"stopped at its cap of {res['cap']} model calls"
    elif _COLLIDED.search(text):
        line = next((ln for ln in text.splitlines() if _COLLIDED.search(ln)), "")
        res.update(status="collided", reason=line.strip()[:300])
    elif not rid:
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        res.update(status="refused" if rc == 3 else "failed", reason=" ".join(lines[-3:])[:300] or f"exit {rc}")
    else:
        res.update(status="failed", reason=_why_failed(meta, err, rc)[:300])
    usage = meta.get("usage") or {}
    aux = (usage.get("auxiliary") or {}).get("api_calls") or 0
    res["aux_calls"] = aux
    res["cost_usd"] = meta.get("cost_usd")
    tot = usage.get("total_including_auxiliary") or {}
    if tot.get("estimated_cost_usd") is not None:
        res["cost_usd_incl_aux"] = tot.get("estimated_cost_usd")
    return res


def _why_failed(meta: dict, err: str, rc) -> str:
    """The reason a timeline that started failed: its error, how its turn ended, its last words; never the
    'rehearsing in' banner the child prints first."""
    if meta.get("error") or meta.get("refused"):
        return str(meta.get("error") or meta.get("refused"))
    parts = []
    ter = (meta.get("usage") or {}).get("turn_exit_reason")
    if ter:
        parts.append(str(ter))
    final = next((ln.strip() for ln in str(meta.get("agent_final_text") or "").splitlines() if ln.strip()), "")
    if final:
        parts.append(final)
    if not parts:
        banner = re.compile(re.escape(names.PRODUCT) + r" " + re.escape(names.RID_PREFIX) + r"\S+: rehearsing in ")
        parts.extend(ln.strip() for ln in err.splitlines()[-3:] if ln.strip() and not banner.match(ln.strip()))
    return "; ".join(parts + [f"exit {rc}"])


def _run_batch(children, cwd, timeout, progress, together: bool):
    if together:
        for c in children:
            c.start(cwd)
        deadline = time.monotonic() + timeout
        left = list(children)
        while left:
            for c in list(left):
                if c.proc.poll() is not None or time.monotonic() > deadline:
                    c.wait(deadline)
                    left.remove(c)
                    progress(c)
            time.sleep(0.05)
    else:
        for c in children:
            c.start(cwd)
            c.wait(time.monotonic() + timeout)
            progress(c)


def run_fork(task, timelines: list, workspace: str, max_calls=DEFAULT_MAX_CALLS, max_plan_calls=DEFAULT_MAX_PLAN_CALLS,
             timeout=DEFAULT_TIMEOUT, toolsets=None, sequential=False, out=None, boot: str | None = None) -> dict:
    """Start every timeline at once (or one after another), wait, write fork.json. Returns the record.
    ``boot``: the Python boot string each timeline process starts with (default _BOOT; see _child_argv)."""
    out = out or sys.stderr
    ws = os.path.realpath(workspace)
    if not os.path.isdir(ws):
        raise state.Refused(f"workspace {ws} is not a directory")
    if not timelines:
        raise ValueError("a fork needs at least one timeline")
    plan = [t for t in timelines if is_plan_provider(t.get("provider"))]
    if len(plan) > MAX_PLAN_TIMELINES:
        raise ValueError(f"at most {MAX_PLAN_TIMELINES} timeline may use the Claude-subscription provider "
                         f"(got {len(plan)}: {', '.join(t['name'] for t in plan)})")
    _dedupe(timelines)
    for t in timelines:
        pin_provider(t)
    prompts = {t["name"]: prompt_for(task, t) for t in timelines}
    fid, fdir = _claim_fork_dir()
    rec = {"schema": FORK_SCHEMA, "id": fid, "plugin": names.PLUGIN_NAME, "version": names.VERSION,
           "task": task, "workspace": ws, "started": state.now_iso(), "ended": None, "status": "running",
           "mode": "one after another" if sequential else "at once", "pid": os.getpid(),
           "max_calls": max_calls, "max_plan_calls": max_plan_calls, "timeout": timeout, "collisions": [],
           "timelines": [dict(t, own_prompt=bool(t.get("prompt")), prompt=prompts[t["name"]]) for t in timelines]}
    _save_fork(fdir, rec)

    def cap_for(t):
        return min(max_calls, max_plan_calls) if is_plan_provider(t.get("provider")) else max_calls

    def mk(t, first=False):
        argv = _child_argv(t, prompts[t["name"]], ws, toolsets, boot)
        if first and t["name"] in (os.environ.get(TEST_COLLIDE_ENV) or "").split(","):
            argv = [sys.executable, "-c", "import sys; sys.stderr.write('sqlite3.OperationalError: database is "
                                          "locked (test seam)\\n'); sys.exit(1)"]
        return _Child(t, fdir, fid, argv, cap_for(t))

    def progress(c):
        rc = c.proc.returncode
        out.write(f"  {c.t['name']}: {'stopped (timeout)' if c.timed_out else 'exit ' + str(rc)} "
                  f"after {(c.t1 or time.monotonic()) - c.t0:.1f} s\n")
        out.flush()

    how = "one after another" if sequential else "at once"
    from . import report  # imported here, as in cmd_apply
    banner = report.wrap_parts([f"{names.PRODUCT} fork {fid}: {len(timelines)} timelines {how}", f"in {ws}",
                                "(nothing will happen for real)"], report._width(None), sep=" ")
    out.write("\n".join(banner) + "\n  " + ", ".join(t["name"] for t in timelines) + "\n")
    for t in timelines:
        if t.get("provider_from") not in (None, "given", "home default model"):
            out.write(f"  {t['name']}: {t['model']} on {t.get('provider') or 'auto'}, {t['provider_from']}\n")
    out.flush()
    children = [mk(t, first=True) for t in timelines]
    t0, t0_clock = time.monotonic(), time.time()
    _run_batch(children, ws, timeout, progress, together=not sequential)
    results = {c.t["name"]: _outcome(c) for c in children}
    batch_wall = time.monotonic() - t0
    rec["wall_seconds_together"] = round(batch_wall, 2)
    collided = [c for c in children if results[c.t["name"]]["status"] == "collided"]
    if collided and not sequential:
        rec["collisions"] = [{"timeline": c.t["name"], "rid": results[c.t["name"]]["rid"],
                              "reason": results[c.t["name"]].get("reason")} for c in collided]
        rec["mode"] = "at once, then one after another"
        out.write(f"  {len(collided)} timeline(s) collided when run at once ("
                  + "; ".join(f"{x['timeline']}: {x['reason']}" for x in rec["collisions"])
                  + "): running them again one after another\n")
        out.flush()
        again = []
        for c in collided:
            os.replace(c.out_path, c.out_path + ".collided")
            os.replace(c.err_path, c.err_path + ".collided")
            if os.path.exists(c.calls_path):
                os.replace(c.calls_path, c.calls_path + ".collided")
            again.append(mk(c.t))
        _run_batch(again, ws, timeout, progress, together=False)
        for c in again:
            results[c.t["name"]] = dict(_outcome(c), rerun="one after another")
    wall = time.monotonic() - t0
    slept = (time.time() - t0_clock) - wall  # macOS's monotonic clock stops while the computer sleeps
    if slept > 30:
        rec["slept_seconds"] = round(slept)
    for t in rec["timelines"]:
        t.update(results[t["name"]])
    ok = [t for t in rec["timelines"] if t["status"] in ("finished", "capped")]
    rec.update(ended=state.now_iso(), wall_seconds=round(wall, 2),
               slowest_seconds=max((t["seconds"] for t in rec["timelines"]), default=0),
               status="finished" if ok else "failed",
               dropped=[{"timeline": t["name"], "model": t.get("model"), "reason": t.get("reason")}
                        for t in rec["timelines"] if t["status"] == "dropped"])
    _save_fork(fdir, rec)
    return rec


# ---------------------------------------------------------------- CLI
COMMANDS = ("fork", "compare")


def add_parsers(subs) -> None:
    """Add `fork` and `compare` to the `hermes clotho` subcommands."""
    p = subs.add_parser("fork", help="run one task several ways at once (a model or an instruction per timeline), "
                                     "compare the timelines, apply one")
    p.add_argument("task", nargs="?", default=None)
    p.add_argument("--models", default=None,
                   help="comma-separated [name=]model[@provider], e.g. deepseek/deepseek-v4-flash,"
                        "moonshotai/kimi-k2.6 (aliases: " + ", ".join(ALIASES) + ")")
    p.add_argument("--variants", default=None, help="YAML file: same model, different instructions per timeline")
    p.add_argument("--workspace", default=None, help="project folder (default: current folder)")
    p.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                   help=f"model calls per timeline before it is told to stop (default {DEFAULT_MAX_CALLS})")
    p.add_argument("--max-plan-calls", type=int, default=DEFAULT_MAX_PLAN_CALLS,
                   help=f"cap for a Claude-subscription timeline (default {DEFAULT_MAX_PLAN_CALLS})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per timeline (default 900)")
    p.add_argument("--toolsets", default=None)
    p.add_argument("--sequential", action="store_true", help="run the timelines one after another")
    p.add_argument("--json", action="store_true", help="print the comparison as JSON")
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--expand", action="store_true", help="list every difference")
    p = subs.add_parser("compare", help="compare the timelines of a fork side by side")
    p.add_argument("id", nargs="?", default="last")
    p.add_argument("--json", action="store_true")
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--expand", action="store_true", help="list every difference")


def timelines_from_args(args) -> list:
    models = [m for m in (args.models or "").split(",") if m.strip()]
    if args.variants:
        if len(models) > 1:
            raise ValueError("--variants runs one model with different instructions; give at most one --models")
        dm = parse_model(models[0]) if models else {"model": None, "provider": None}
        return parse_variants(args.variants, dm["model"], dm["provider"])
    if not models:
        raise ValueError("give --models a,b,c or --variants file.yaml")
    return [parse_model(m) for m in models]


def handle(args) -> int:
    from . import compare
    cmd = getattr(args, "clotho_cmd", None)
    if cmd == "compare":
        fid = resolve_fork(args.id)
        if not fid:
            print(f"no fork matches '{args.id}'", file=sys.stderr)
            return 2
        return compare.print_compare(fid, as_json=args.json, width=args.width, expand=args.expand)
    if cmd == "fork":
        try:
            tls = timelines_from_args(args)
            rec = run_fork(args.task, tls, args.workspace or os.getcwd(), max_calls=args.max_calls,
                           max_plan_calls=args.max_plan_calls, timeout=args.timeout, toolsets=args.toolsets,
                           sequential=args.sequential)
        except (ValueError, OSError) as exc:
            print(f"{names.PRODUCT} fork: {exc}", file=sys.stderr)
            return 2
        except state.Refused as exc:
            print(f"{names.PRODUCT}: refused: {exc}", file=sys.stderr)
            return 3
        compare.print_compare(rec["id"], as_json=args.json, width=args.width, expand=args.expand)
        return 0 if rec["status"] == "finished" else 1
    return 2


def is_fork_spec(rid) -> bool:
    return bool(rid) and "/" in str(rid) and str(rid).split("/", 1)[0].startswith(FORK_PREFIX[:1])


def cmd_apply(args) -> int:
    """`hermes clotho apply <fork id>/<timeline>`: that timeline's rehearsal through the normal apply
    path (drift-checked, 0 model calls). Refused when another timeline of the fork was already applied."""
    from . import apply as apply_mod
    from . import cli, report
    if state.any_armed():
        print("refusing to apply inside a rehearsal", file=sys.stderr)
        return apply_mod.EXIT_REFUSED
    try:
        fid, t = resolve_timeline(args.id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return apply_mod.EXIT_BAD_ID
    if not t.get("rid") or t.get("status") not in ("finished", "capped"):
        print(f"timeline {t['name']} of {fid} has nothing to apply (status: {t.get('status')}"
              + (f", {t['reason']}" if t.get("reason") else "") + ")", file=sys.stderr)
        return apply_mod.EXIT_BAD_ID
    fdir = fork_dir(fid)
    rec = load_fork(fid)
    others = [a for a in rec.get("applied") or [] if a["timeline"] != t["name"] and a.get("applied", 0) > 0]
    if others:
        print(f"{names.PRODUCT}: refused: timeline {others[0]['timeline']} of {fid} was already applied; "
              f"applying {t['name']} too would mix two futures. Apply its rehearsal by id if you mean it: "
              f"hermes {names.CLI_NAME} apply {t['rid']}", file=sys.stderr)
        return apply_mod.EXIT_REFUSED
    try:
        rc = apply_mod.apply(t["rid"], only=cli._ids(args.only) or None, except_=cli._ids(args.except_) or None,
                             dry_run=args.dry_run, force=getattr(args, "force", False))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return apply_mod.EXIT_BAD_ID
    except RuntimeError as exc:
        print(f"{names.PRODUCT}: refused: {exc}", file=sys.stderr)
        return apply_mod.EXIT_REFUSED
    if not args.dry_run:
        rec.setdefault("applied", []).append({"timeline": t["name"], "rid": t["rid"], "at": state.now_iso(),
                                              "status": rc.get("status"), "applied": rc.get("applied", 0),
                                              "model_calls": rc.get("model_calls")})
        _save_fork(fdir, rec)
    rc["fork"] = {"id": fid, "timeline": t["name"],
                  "others_not_applied": [x["name"] for x in rec["timelines"] if x["name"] != t["name"]]}
    if args.json:
        print(json.dumps(rc, indent=1, ensure_ascii=False, default=str))
    else:
        print(report.fit(f"fork {fid} · timeline {t['name']}", report._width(None)))
        print(report.render_receipt(rc))
        rest = rc["fork"]["others_not_applied"]
        if rest:
            for ln in report.wrap(f"not applied: {', '.join(rest)} (their changes stay in their own "
                                  "rehearsal folders)", report._width(None)):
                print(report._c(ln, "dim", on=report._use_color()))
    return apply_mod.exit_code(rc)
