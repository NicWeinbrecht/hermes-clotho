"""`hermes clotho demo`: Forks end to end in about 10 s, with no API key and no network.

What runs for real: a fresh Downloads folder that is "nearly full" (demo/seed), three timelines through the
fork runner, the capture middleware, the sandboxed shell, the ledgers, the comparison, Apply, and a local
sink that receives what Apply sends. What does not run: the models. Their turns were recorded from real
forks on this same folder (demo/recordings, with the models and the date in manifest.json) and are served
back, call by call, in place of the provider. The screen says so.

Nothing leaves the machine while the demo runs, and the demo shows how it knows:
  - every Python process of the demo runs under a network guard (an audit hook installed before Hermes is
    imported) that refuses and logs any connection or name lookup that is not 127.0.0.1/::1/localhost;
  - HTTP(S)_PROXY points at a local trap that logs every connection it gets, and the model endpoint is a
    local stand-in that logs every request (a model call there would mean the replay failed);
  - sandboxed shell commands have no network at all (clotho.sb);
  - each instrument is checked first with a test connection that it must catch.

Layout of one demo (a temp folder, kept afterwards):
    <tmp>/you/Downloads      the workspace (the recordings say /Users/you/Downloads)
    <tmp>/you/.hermes        a throwaway Hermes home: no keys, title generation off, a "recorded" provider
    <tmp>/stage/             plan, replay logs, network logs, results

Recording new turns (needs your own keys; spends real model calls):
    hermes clotho demo --seed <dir>                 # writes the seeded folder to <dir>/Downloads
    cd <dir>/Downloads && hermes clotho demo --record <out> --models pro,kimi,claude
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capture, fork, names, report, state

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(HERE, "demo")
NEUTRAL = "/Users/you"                     # the home folder the recordings are written against
REPLAY_PROVIDER = "recorded"               # provider name in the demo's Hermes home
RECORDING_SCHEMA = f"{names.PLUGIN_NAME}.demo-recording.v1"
SINK_PORT = 19207                          # the port in the recorded task's summary URL
SINK_TOKEN = "{sink}"                      # the task as stored names its sink like this (the URL is per run)
SINK_URL = f"http://127.0.0.1:{SINK_PORT}"  # what the recorded models were given for {sink}
TRAP_PORT = 19208                          # preferred port of the proxy trap (any free port works)
ENDPOINT_PORT = 19209                      # preferred port of the stand-in model endpoint
TIME_LIMIT = 240                           # seconds before the demo gives up on its stage process
TIMELINE_LIMIT = 150                       # seconds per timeline (the fork stops a slower one by its pid)

ENV_RECORD = names.env("DEMO_RECORD")      # timeline process: folder to write raw turns into
ENV_REPLAY = names.env("DEMO_REPLAY")      # timeline process: folder with <timeline>.jsonl recordings
ENV_ROOT = names.env("DEMO_ROOT")          # what /Users/you stands for during a replay
ENV_SINK = names.env("DEMO_SINK_PORT")     # the sink port during a replay, when not SINK_PORT
ENV_LOGS = names.env("DEMO_LOGS")          # replay logs
ENV_GUARD = names.env("DEMO_GUARD_LOG")    # network guard log; set = guard on in every demo process
ENV_TEST_NO_REPLAY = names.env("DEMO_TEST_NO_REPLAY")  # test seam: timelines get no recording (control)

_KEY_VARS = re.compile(r"(API_KEY|_TOKEN|_SECRET|_PASSWORD|ACCESS_KEY)$|^(OPENROUTER|ANTHROPIC|OPENAI|NOUS|"
                       r"DEEPSEEK|MOONSHOT|GEMINI|GOOGLE|AZURE|AWS|MISTRAL|GROQ|XAI|TOGETHER|FIREWORKS|CLAUDE)_|"
                       r"^HERMES_INFERENCE_", re.I)


ENV_DIR = names.env("DEMO_DIR")            # where demo/ is (the demo's own Hermes home has only the code)


def demo_dir() -> str:
    return os.environ.get(ENV_DIR) or DEMO_DIR


def recordings_dir() -> str:
    return os.environ.get(names.env("DEMO_RECORDINGS")) or os.path.join(demo_dir(), "recordings")


def seed_dir() -> str:
    return os.path.join(demo_dir(), "seed")


# ---------------------------------------------------------------- the network guard
# Installed by _BOOT before anything else is imported, in every Python process the demo starts. It is
# code in a string so that it runs before Hermes; it lives here so that it is read and tested with the rest.
_GUARD = r'''
import json as _j, os as _o, sys as _s, traceback as _tb
_GLOG = _o.environ.get(%(env)r)
_GTRACE = _o.environ.get(%(trace)r)
def _g_local(h):
    h = h.decode() if isinstance(h, bytes) else str(h)
    return h in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
def _g_hook(ev, args):
    if ev not in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.gethostbyname",
                  "socket.gethostbyname_ex", "socket.gethostbyaddr"):
        return
    try:
        if ev == "socket.connect" or ev == "socket.sendto":
            addr = args[1] if ev == "socket.connect" else args[2]
            if not isinstance(addr, tuple):
                return
            what = {"addr": "%%s:%%s" %% (addr[0], addr[1] if len(addr) > 1 else "")}
            if _g_local(addr[0]):
                if _GTRACE:  # debugging aid: where local connections come from; never blocked
                    with open(_GLOG + ".local", "a") as _f:
                        _f.write(_j.dumps(dict(what, pid=_o.getpid(), stack=["%%s:%%s:%%s" %% (_o.path.basename(f.filename), f.lineno, f.name) for f in _tb.extract_stack()[-70:-1]])) + "\n")
                return
        else:
            host = args[0]
            if host is None or _g_local(host):
                return
            what = {"host": host.decode() if isinstance(host, bytes) else str(host)}
    except Exception:
        return
    rec = dict(what, ev=ev, pid=_o.getpid(), control=bool(_s.__dict__.get("_rt_demo_control")),
               stack=["%%s:%%s" %% (_o.path.basename(f.filename), f.lineno) for f in _tb.extract_stack()[-8:-1]])
    try:
        with open(_GLOG, "a") as _f:
            _f.write(_j.dumps(rec) + "\n")
    except OSError:
        pass
    raise PermissionError("%(product)s demo: network is off (%%s)" %% (what.get("addr") or what.get("host")))
if _GLOG:
    _s.addaudithook(_g_hook)
    _s._rt_demo_guard = True
    # Positive control: a connection the guard must refuse, logged as a control. 127.0.0.2 is outside what
    # the guard allows, yet loopback: even without the guard it could not leave the machine.
    _s._rt_demo_control = True
    try:
        import socket as _sk
        _c = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        _c.settimeout(0.2)
        try:
            _c.connect(("127.0.0.2", 9))
        finally:
            _c.close()
    except Exception:
        pass
    _s._rt_demo_control = False
''' % {"env": ENV_GUARD, "trace": names.env("DEMO_TRACE"), "product": names.PRODUCT}

# The process the demo starts: the stock Hermes CLI, with the guard first. A timeline process (it calls
# run_oneshot) also gets the fork's model-call cap and the demo's replay or recorder, right after the
# rehearsal is armed. If any of that cannot be installed the timeline stops before a model call.
_BOOT = _GUARD + r'''
import os, sys
src, fork_py = sys.argv[1], os.path.realpath(sys.argv[2])
demo_py = os.path.join(os.path.dirname(fork_py), "demo.py")
sys.path.insert(0, src)
sys.argv = ["hermes"] + sys.argv[3:]
import hermes_cli.oneshot as _oneshot
_orig = _oneshot.run_oneshot
def _mod(path, attr):
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and hasattr(m, attr) and os.path.realpath(f) == path:
            return m
def _run(*a, **k):
    try:
        _mod(fork_py, "install_cap").install_cap()
        _mod(demo_py, "install_turns").install_turns()
    except Exception as exc:
        sys.stderr.write("cannot install the model-call cap or the recorded turns (%s: %s); not starting the model\n" % (type(exc).__name__, exc))
        return 3
    return _orig(*a, **k)
_oneshot.run_oneshot = _run
from hermes_cli.main import main
main()
'''


# ---------------------------------------------------------------- responses to JSON and back
def dump_obj(o, depth=0):
    """A provider response (SimpleNamespace, pydantic, dict, list) as JSON, tagged so it can be rebuilt."""
    if depth > 40:
        return repr(o)
    if isinstance(o, types.SimpleNamespace):
        return {"__ns__": {k: dump_obj(v, depth + 1) for k, v in vars(o).items()}}
    if hasattr(o, "model_dump") and not isinstance(o, type):
        return {"__pyd__": f"{type(o).__module__}.{type(o).__name__}", "data": o.model_dump()}
    if isinstance(o, dict):
        return {str(k): dump_obj(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [dump_obj(v, depth + 1) for v in o]
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    return {"__repr__": repr(o)[:300]}


def load_obj(o):
    if isinstance(o, dict):
        if "__ns__" in o:
            return types.SimpleNamespace(**{k: load_obj(v) for k, v in o["__ns__"].items()})
        if "__pyd__" in o:
            import importlib
            mod, _, cls = o["__pyd__"].rpartition(".")
            try:
                return getattr(importlib.import_module(mod), cls).model_validate(o["data"])
            except Exception:
                return load_obj(o["data"])
        return {k: load_obj(v) for k, v in o.items()}
    if isinstance(o, list):
        return [load_obj(v) for v in o]
    return o


def rewrite(o, pairs):
    """Every string in o with each (old, new) pair replaced, in order."""
    if isinstance(o, str):
        for a, b in pairs:
            if a and a in o:
                o = o.replace(a, b)
        return o
    if isinstance(o, dict):
        return {k: rewrite(v, pairs) for k, v in o.items()}
    if isinstance(o, list):
        return [rewrite(v, pairs) for v in o]
    return o


_IDS = re.compile(re.escape(names.RID_PREFIX) + r"\d{8}-\d{6}-[0-9a-f]{4}|" + re.escape(fork.FORK_PREFIX)
                  + r"\d{8}-\d{6}-[0-9a-f]{4}|call_[A-Za-z0-9_-]+|toolu_[A-Za-z0-9_-]+")


_MONTHS = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")


def _plain(content) -> str:
    """A message's text: providers that cache prompts send it as a list of parts with cache markers."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
        if parts:
            return "".join(parts)
    return json.dumps(content, sort_keys=True, default=str)


_NOTICE = re.compile(re.escape(names.RESULT_TAG) + r'[^"]*')


def text_hash(text, pairs) -> str:
    """Hash of a tool result with paths made neutral and ids, numbers and spacing left out: two runs of the
    same command on the same folder hash alike even when dates, sizes or free space differ. The plugin's own
    notice in a captured call's result (from its tag to the end of that string) is left out too: it is the
    plugin's wording, not something the world answered, and it may change between versions."""
    text = _plain(text)
    t = rewrite(text, pairs)
    t = _NOTICE.sub("<notice>", t)
    t = _IDS.sub("<id>", t)
    t = _MONTHS.sub("<mon>", t)
    t = re.sub(r"\d+", "#", t)
    t = " ".join(t.split())
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def _new_tool_results(request) -> tuple[int, list]:
    msgs = (request or {}).get("messages") if isinstance(request, dict) else None
    if not isinstance(msgs, list):
        return 0, []
    last = max((i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "assistant"), default=-1)
    tools = [m.get("content") for m in msgs[last + 1:] if isinstance(m, dict) and m.get("role") == "tool"]
    return len(msgs), tools


def _append(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def record_pairs(ws: str, home: str, user_home: str, user: str) -> list:
    """Real -> neutral, longest first: the workspace, the Hermes home, the user's home, the user name."""
    pairs = []
    for real, neutral in ((ws, NEUTRAL + "/Downloads"), (os.path.dirname(ws), NEUTRAL),
                          (home, NEUTRAL + "/.hermes"), (user_home, NEUTRAL)):
        for p in {real, os.path.realpath(real)}:
            if p and p != "/":
                pairs.append((p, neutral))
    pairs.sort(key=lambda x: -len(x[0]))
    if user and len(user) > 2:
        pairs.append((user, "you"))
    return pairs


def replay_pairs(root: str, sink_port: int | None) -> list:
    pairs = [(SINK_TOKEN, f"http://127.0.0.1:{sink_port or SINK_PORT}"), (NEUTRAL, root)]
    if sink_port and sink_port != SINK_PORT:
        pairs += [(f"127.0.0.1:{SINK_PORT}", f"127.0.0.1:{sink_port}"), (f"localhost:{SINK_PORT}",
                                                                          f"localhost:{sink_port}")]
    return pairs


def _hash_pairs_at_replay(root: str, sink_port) -> list:
    """Replay-time tool results back to the recording's terms, so their hashes can be compared."""
    pairs = [(root, NEUTRAL), (os.path.realpath(root), NEUTRAL)]
    if sink_port and sink_port != SINK_PORT:
        pairs.append((f"127.0.0.1:{sink_port}", f"127.0.0.1:{SINK_PORT}"))
    try:
        u = getpass.getuser()
        if u and len(u) > 2:
            pairs.append((u, "you"))
    except Exception:
        pass
    return pairs


# ---------------------------------------------------------------- recorder and replay (timeline process)
_turns = {"installed": False, "rows": None, "i": 0, "lock": threading.Lock(), "header": None,
          "sessions": {}, "next": {}}


def _session_no(kw) -> int:
    """0 for the first Hermes session that calls the model in this process, 1 for the next (a subagent),
    and so on. Each session, and within it the task and Hermes' background review, gets its own recorded
    calls, in order."""
    sid = str(kw.get("session_id") or "")
    with _turns["lock"]:
        return _turns["sessions"].setdefault(sid, len(_turns["sessions"]))


def _origin() -> str:
    return capture._origin() or "task"


def _timeline() -> str:
    return os.environ.get(fork.ENV_TIMELINE) or "timeline"


def record_middleware(**kw):
    """Innermost llm_execution middleware while recording: the real provider answers; its answer and the
    tool results the model was shown are written down. Never changes the answer."""
    next_call, request = kw.get("next_call"), kw.get("request")
    t0 = time.time()
    resp = next_call(request)
    try:
        n, tools = _new_tool_results(request)
        sess = _session_no(kw)
        with _turns["lock"]:
            i = _turns["i"]
            _turns["i"] += 1
        _append(os.path.join(os.environ[ENV_RECORD], _timeline() + ".raw.jsonl"), {
            "i": i, "session": sess, "origin": _origin(), "api_call_count": kw.get("api_call_count"),
            "t": round(t0, 3), "seconds": round(time.time() - t0, 2), "model": kw.get("model"),
            "provider": kw.get("provider"), "api_mode": kw.get("api_mode"), "n_messages": n,
            "tool_results": tools, "type": f"{type(resp).__module__}.{type(resp).__name__}",
            "response": dump_obj(resp)})
    except Exception as exc:  # recording is best effort; the run itself must not change
        try:
            _append(os.path.join(os.environ[ENV_RECORD], _timeline() + ".raw.jsonl"),
                    {"record_failed": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    return resp


def _stop(text, model):
    return fork._synthetic_stop(text, model)


def replay_middleware(**kw):
    """Innermost llm_execution middleware during the demo: answers call i with recorded turn i and never
    calls the provider. Checks the request against the recording (message count, tool-result hashes)."""
    request, model = kw.get("request"), kw.get("model")
    logs = os.environ.get(ENV_LOGS) or "."
    log = os.path.join(logs, _timeline() + ".replay.jsonl")
    sess, origin = _session_no(kw), _origin()
    with _turns["lock"]:
        i = _turns["i"]
        _turns["i"] += 1
        k = _turns["next"].get((sess, origin), 0)
        _turns["next"][(sess, origin)] = k + 1
    if not getattr(sys, "_rt_demo_guard", False):
        _append(log, {"i": i, "error": "network guard not installed in this process; nothing replayed"})
        return _stop(f"{names.RESULT_TAG} The network guard is not on in this process, so the recorded turns "
                     "are not replayed. Stop now.", model)
    head = _turns["header"] or {}
    rows = [r for r in _turns["rows"] if int(r.get("session") or 0) == sess and (r.get("origin") or "task") == origin]
    n, tools = _new_tool_results(request)
    base = {"i": i, "session": sess, "origin": origin, "k": k, "api_call_count": kw.get("api_call_count"),
            "n_messages": n}
    if k >= len(rows):
        if head.get("capped") and sess == 0 and origin == "task":  # the live timeline hit its cap here: the same stop as then
            _append(log, dict(base, served="cap stop"))
            return _stop(f"{names.RESULT_TAG} This timeline reached its cap of {head.get('cap')} model calls. "
                         "Stop now: no more tool calls. The rehearsal ends here.", model)
        last = ((request or {}).get("messages") or [{}])[-1] if isinstance(request, dict) else {}
        _append(log, dict(base, served="end of recording", beyond=True, last_role=last.get("role"),
                          last_text=str(last.get("content"))[:400]))
        return _stop(f"{names.RESULT_TAG} The recording of this timeline ends here. Stop now.", model)
    row = rows[k]
    pairs = _hash_pairs_at_replay(os.environ.get(ENV_ROOT) or "", _sink_port_env())
    got = [text_hash(t, pairs) for t in tools]
    if got != (row.get("tool_hashes") or []) and os.environ.get(names.env("DEMO_TRACE")):
        base["tool_texts"] = [str(t)[:3000] for t in tools]
    _append(log, dict(base, served="recorded", n_messages_recorded=row.get("n_messages"), tool_hashes=got,
                      tool_hashes_recorded=row.get("tool_hashes"),
                      match=got == (row.get("tool_hashes") or []) and n == row.get("n_messages")))
    resp = rewrite(row["response"], replay_pairs(os.environ.get(ENV_ROOT) or NEUTRAL, _sink_port_env()))
    return load_obj(resp)


def _sink_port_env():
    try:
        return int(os.environ.get(ENV_SINK) or SINK_PORT)
    except ValueError:
        return SINK_PORT


def load_recording(path: str) -> tuple[dict, list]:
    head, rows = {}, []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            x = json.loads(line)
            if "schema" in x and not head:
                head = x
            else:
                rows.append(x)
    return head, rows


def install_turns() -> None:
    """Called in a timeline process right after the rehearsal is armed: append the recorder or the
    replay to the llm_execution chain (innermost, so the cap and the capture layer still see every call)."""
    if _turns["installed"]:
        return
    from hermes_cli.plugins import _delivery_manager
    chain = _delivery_manager()._middleware.setdefault("llm_execution", [])
    if os.environ.get(ENV_REPLAY):
        if os.environ.get(ENV_TEST_NO_REPLAY):  # control: no recording, so the model endpoint is called
            _turns["installed"] = True
            return
        path = os.path.join(os.environ[ENV_REPLAY], _timeline() + ".jsonl")
        _turns["header"], _turns["rows"] = load_recording(path)
        if not _turns["rows"]:
            raise RuntimeError(f"no recorded turns in {path}")
        chain.append(replay_middleware)
    elif os.environ.get(ENV_RECORD):
        chain.append(record_middleware)
    _turns["installed"] = True


# ---------------------------------------------------------------- the seeded folder
def precious() -> list:
    with open(os.path.join(seed_dir(), "precious.txt")) as f:
        return [ln.strip() for ln in f if ln.strip()]


def seed_files() -> list:
    with open(os.path.join(seed_dir(), "downloads.json")) as f:
        return json.load(f)["files"]


def make_seed(dest: str, now=None) -> int:
    """Write the seeded Downloads folder at dest (must not exist); modification times are each file's age
    counted back from now."""
    now = time.time() if now is None else now
    os.makedirs(dest)
    for x in seed_files():
        p = os.path.join(dest, x["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(x["text"])
        t = now - x["age_days"] * 86400
        os.utime(p, (t, t))
    return len(seed_files())


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _tree_shas(d: str) -> dict:
    out = {}
    for dp, _dn, fn in os.walk(d):
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as fh:
                    out.setdefault(_sha_bytes(fh.read()), []).append(os.path.relpath(p, d))
    return out


def outcome(folder: str, elsewhere=()) -> dict:
    """What a folder kept of the seed. A file whose bytes are found nowhere in the folder is gone from it;
    found in one of the elsewhere folders (the Trash), it is set aside; found nowhere, it is lost."""
    have = _tree_shas(folder)
    other = {}
    for d in elsewhere:
        if os.path.isdir(d):
            other.update(_tree_shas(d))
    gone = [x["path"] for x in seed_files() if _sha_bytes(x["text"].encode()) not in have]
    lost = [p for p, x in zip([y["path"] for y in seed_files()], seed_files())
            if p in gone and _sha_bytes(x["text"].encode()) not in other]
    pre = precious()
    return {"files": sum(len(v) for v in have.values()), "gone": len(gone), "lost": len(lost),
            "set_aside": len(gone) - len(lost),
            "precious_lost": [p for p in pre if p in lost], "precious_kept": [p for p in pre if p not in gone],
            "precious_set_aside": [p for p in pre if p in gone and p not in lost]}


def timeline_outcome(t: dict) -> dict:
    """Deletes in a timeline's ledger and what its clone lost of the seed."""
    rid = t.get("rid")
    if not rid:
        return {}
    rd = state.rehearsal_dir(rid)
    rows = []
    try:
        with open(os.path.join(rd, "ledger.jsonl")) as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except OSError:
        pass
    out = outcome(os.path.join(rd, "work"), (os.path.join(rd, "home-overlay", "home"), os.path.join(rd, "outside")))
    out["deletes"] = sum(1 for r in rows if r.get("class") == "file" and r.get("change") == "deleted")
    out["effects"] = len(rows)
    out["web"] = [(r.get("target") or {}).get("url") for r in rows if r.get("class") == "http"]
    return out


# ---------------------------------------------------------------- local servers (in the demo process)
class _Log:
    def __init__(self, path):
        self.path, self.lock, self.rows = path, threading.Lock(), []

    def add(self, rec):
        with self.lock:
            self.rows.append(rec)
            _append(self.path, rec)


def _serve(port, handler_cls) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_sink(port: int, log: _Log) -> ThreadingHTTPServer:
    class Sink(BaseHTTPRequestHandler):
        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            log.add({"ts": round(time.time(), 3), "method": self.command, "path": self.path, "body": body})
            out = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

        def log_message(self, *a):
            pass
    return _serve(port, Sink)


def start_trap(port: int, log: _Log) -> ThreadingHTTPServer:
    """Proxy and model endpoint in one: logs every request line it gets and answers 502, so nothing that
    reaches it goes any further."""
    class Trap(BaseHTTPRequestHandler):
        def _do(self):
            log.add({"ts": round(time.time(), 3), "line": self.requestline[:300],
                     "control": self.headers.get("X-Demo-Control") == "1"})
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_CONNECT = do_HEAD = _do

        def log_message(self, *a):
            pass
    return _serve(port, Trap)


def start_endpoint(port: int, log: _Log) -> ThreadingHTTPServer:
    """The demo home's model endpoint: a local stand-in that is not a model. A model call reaching it means
    the replay failed (logged, answered 503); Hermes' own metadata probes are logged and answered 404."""
    class Endpoint(BaseHTTPRequestHandler):
        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            call = self.command == "POST" and any(k in self.path for k in ("completions", "messages", "responses"))
            log.add({"ts": round(time.time(), 3), "line": self.requestline[:300], "model_call": call})
            # 400, not 5xx: a client does not retry it, so a failed replay ends fast instead of backing off
            body = json.dumps({"error": {"message": f"{names.PRODUCT} demo: this endpoint is not a model",
                                         "type": "invalid_request_error"}}).encode() if call else b""
            self.send_response(400 if call else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _do

        def log_message(self, *a):
            pass
    return _serve(port, Endpoint)


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the servers bind: a port in TIME_WAIT is free
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_port(preferred: int) -> int:
    if preferred and _port_free(preferred):
        return preferred
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def trap_control(port: int) -> None:
    """Positive control for the trap: one request through it as a proxy, the way a client would go out."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"}))
    try:
        opener.open(urllib.request.Request("http://example.invalid/demo-trap-check",
                                           headers={"X-Demo-Control": "1"}), timeout=5).read()
    except Exception:
        pass


# ---------------------------------------------------------------- the throwaway Hermes home
def _source_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def make_home(home: str, endpoint_port: int, models: list, src_home: str | None = None) -> None:
    """A Hermes home for the demo: Hermes' installed runtime cloned copy-on-write from the current home,
    this plugin copied in, no .env, no keys."""
    src = os.path.realpath(src_home or _source_home())
    os.makedirs(home)
    home = os.path.realpath(home)
    if not os.path.isdir(os.path.join(src, "installs")):
        raise state.Refused(f"{src} has no installs/ folder: run the demo from a Hermes that has been set up")
    for d in ("installs", "source-checks"):
        if os.path.isdir(os.path.join(src, d)):
            r = subprocess.run(["/bin/cp", "-c", "-R", os.path.join(src, d), os.path.join(home, d)],
                               capture_output=True, text=True)
            if r.returncode != 0:  # not APFS: a plain copy (slower, same result)
                shutil.copytree(os.path.join(src, d), os.path.join(home, d), symlinks=True, dirs_exist_ok=True)
    for dp, _dn, fn in os.walk(home):  # the runtime's records name the home they were made in
        depth = dp[len(home):].count(os.sep)
        if depth > 3:
            continue
        for f in fn:
            if f == "facts.json" or (os.path.basename(dp) == "source-checks" and f.endswith(".json")):
                p = os.path.join(dp, f)
                with open(p) as fh:
                    s = fh.read()
                if src in s:
                    with open(p, "w") as fh:
                        fh.write(s.replace(src, home))
    dest = os.path.join(home, "plugins", names.PLUGIN_NAME)
    os.makedirs(dest)
    for f in os.listdir(HERE):
        if f == "plugin.yaml" or f.endswith(".py") or f.endswith(".sb"):
            shutil.copy2(os.path.join(HERE, f), dest)
    lines = ["plugins:", "  enabled:", f"    - {names.PLUGIN_NAME}", "  disabled: []",
             "auxiliary:", "  title_generation:", "    enabled: false",
             "providers:", f"  {REPLAY_PROVIDER}:", f"    api: http://127.0.0.1:{endpoint_port}/v1", "    models:"]
    # a context length per model, so Hermes does not probe the endpoint for one
    lines += [f"      {json.dumps(m)}:\n        context_length: 200000" for m in models]
    lines += ["gateway:", "  unmanaged_idle_exit_seconds: 60", ""]
    with open(os.path.join(home, "config.yaml"), "w") as f:
        f.write("\n".join(lines))


def clean_env(**extra) -> dict:
    """The environment of the demo's processes: no model keys, no provider overrides."""
    env = {k: v for k, v in os.environ.items() if not _KEY_VARS.search(k)}
    for k in ("PYTHONHOME", "PYTHONPATH", "FORCE_COLOR"):
        env.pop(k, None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _boot_argv(args: list) -> list:
    """The same interpreter as this process (Hermes' own, so the child does not relaunch itself)."""
    return [sys.executable, "-B"] + (["-I"] if sys.flags.isolated else []) + \
        ["-c", _BOOT, fork._hermes_src(), os.path.abspath(fork.__file__), *args]


# ---------------------------------------------------------------- display
def _w():
    return report._width(None)


def _say(text="", *style, stream=None):
    stream = stream or sys.stdout
    on = report._use_color(stream)
    if not text:
        stream.write("\n")
    else:
        for ln in report.wrap(text, _w()):
            stream.write(report._c(ln, *style, on=on) + "\n")
    stream.flush()


def _rule(title, stream=None):
    stream = stream or sys.stdout
    on = report._use_color(stream)
    w = _w()
    t = report.fit(f"── {title} ", w)
    stream.write(report._c(t + "─" * max(0, w - report.vwidth(t)), "bold", on=on) + "\n")
    stream.flush()


def manifest() -> dict:
    p = os.path.join(recordings_dir(), "manifest.json")
    if not os.path.isfile(p):
        raise state.Refused(f"the demo's recorded files are missing ({p}); reinstall the plugin")
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------- `hermes clotho demo`
def add_parser(subs) -> None:
    p = subs.add_parser("demo", help="Forks end to end in about 10 s: recorded model turns, real engine, "
                                     "no key, no network")
    p.add_argument("--apply", default=None, help="timeline to apply (default: the one that loses no precious file, "
                                                 "else the one that loses fewest)")
    p.add_argument("--dir", default=None, help="folder for the demo's files (default: a new temp folder)")
    p.add_argument("--sink-port", type=int, default=SINK_PORT)
    p.add_argument("--trap-port", type=int, default=TRAP_PORT)
    p.add_argument("--endpoint-port", type=int, default=ENDPOINT_PORT)
    p.add_argument("--stage", default=None, help=argparse.SUPPRESS)
    p.add_argument("--seed", default=None, metavar="DIR", help="only write the seeded Downloads folder into DIR")
    p.add_argument("--record", default=None, metavar="OUT",
                   help="record new turns from live models in the current folder (spends real model calls)")
    p.add_argument("--models", default="pro,kimi,claude", help="with --record: the timelines")
    p.add_argument("--max-plan-calls", type=int, default=5, help="with --record: cap for a Claude-plan timeline")


def handle(args) -> int:
    try:
        if args.stage:
            return stage(args.stage)
        if args.seed:
            n = make_seed(os.path.join(os.path.abspath(args.seed), "Downloads"))
            print(f"{n} files in {os.path.join(os.path.abspath(args.seed), 'Downloads')}")
            return 0
        if args.record:
            return record(args)
        return run_demo(args)
    except state.Refused as exc:
        print(f"{names.PRODUCT} demo: {exc}", file=sys.stderr)
        return 3


def run_demo(args) -> int:
    t_start = time.monotonic()
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"):
        raise state.Refused("the demo needs macOS (the shell preview runs under sandbox-exec)")
    man = manifest()
    tls = man["timelines"]
    base = os.path.realpath(args.dir) if args.dir else os.path.realpath(
        tempfile.mkdtemp(prefix=f"{names.PLUGIN_NAME}-demo-"))
    os.makedirs(base, exist_ok=True)
    if os.listdir(base):
        raise state.Refused(f"{base} is not empty")
    root, sd = os.path.join(base, "you"), os.path.join(base, "stage")
    ws, home = os.path.join(root, "Downloads"), os.path.join(root, ".hermes")
    os.makedirs(sd)
    sink_log, trap_log = _Log(os.path.join(sd, "sink.jsonl")), _Log(os.path.join(sd, "trap.jsonl"))
    ep_log = _Log(os.path.join(sd, "endpoint.jsonl"))
    guard_log = os.path.join(sd, "guard.jsonl")
    open(guard_log, "a").close()
    sink_port = _pick_port(args.sink_port)
    trap_port = _pick_port(args.trap_port if args.trap_port != sink_port else 0)
    ep_port = _pick_port(args.endpoint_port if args.endpoint_port not in (sink_port, trap_port) else 0)

    _rule(f"{names.PRODUCT} demo")
    when = man.get("recorded_on") or "?"
    _say(f"RECORDED MODEL TURNS. The {len(tls)} timelines replay model turns recorded on {when} from one "
         "real fork of this task:", "bold")
    nw = max(len(t["name"]) for t in tls)
    for t in tls:
        _say(f"  {t['name'].ljust(nw)}  {t['recorded_model']}, {t['recorded_provider']}")
    _say("No key, no model call, nothing leaves this machine. Capture, the sandboxed shell, the ledgers, the "
         "comparison and Apply run for real.", "bold")
    _say()
    n = make_seed(ws)
    pre = precious()
    _say(f"folder  {ws}")
    _say(f"        {n} files, disk nearly full, {len(pre)} of them precious (the model is not told which)", "dim")
    prompt = rewrite(man["task"], replay_pairs(root, sink_port))  # the task as the timelines get it
    _say(f"task    {' '.join(prompt.split())}", "dim")
    before = outcome(ws)
    tree_before = _tree_hash(ws)

    sink = start_sink(sink_port, sink_log)
    trap = start_trap(trap_port, trap_log)
    endpoint = start_endpoint(ep_port, ep_log)
    trap_control(trap_port)
    make_home(home, ep_port, sorted({t["model"] for t in tls}))
    trap_url = f"http://127.0.0.1:{trap_port}"
    env = clean_env(HERMES_HOME=home, HOME=root, PYTHONDONTWRITEBYTECODE="1",
                    HTTP_PROXY=trap_url, HTTPS_PROXY=trap_url, ALL_PROXY=trap_url, http_proxy=trap_url,
                    https_proxy=trap_url, all_proxy=trap_url, NO_PROXY="127.0.0.1,localhost",
                    no_proxy="127.0.0.1,localhost")
    env.update({ENV_GUARD: guard_log, ENV_REPLAY: recordings_dir(), ENV_DIR: demo_dir(), ENV_ROOT: root,
                ENV_SINK: str(sink_port), ENV_LOGS: os.path.join(sd, "replay")})
    if report._use_color():
        env["FORCE_COLOR"] = "1"
    os.makedirs(os.path.join(sd, "replay"))
    plan = {"prompt": prompt, "workspace": ws, "root": root, "sink_port": sink_port, "apply": args.apply,
            "timelines": [{"name": t["name"], "model": t["model"]} for t in tls], "base": base}
    with open(os.path.join(sd, "plan.json"), "w") as f:
        json.dump(plan, f, indent=1)
    _say()
    try:
        p = subprocess.run(_boot_argv([names.CLI_NAME, "demo", "--stage", sd]), cwd=ws, env=env,
                           stdin=subprocess.DEVNULL, timeout=TIME_LIMIT)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
        _stop_leftovers(home, base)
    res = {}
    try:
        with open(os.path.join(sd, "stage.json")) as f:
            res = json.load(f)
    except (OSError, ValueError):
        pass
    for srv in (sink, trap, endpoint):
        srv.shutdown()
        srv.server_close()

    _say()
    _rule("the sink afterwards")
    if not sink_log.rows:
        _say("no requests", "dim")
        for u in res.get("left_unticked") or []:
            if u.get("class") == "command" and "curl" in (u.get("command") or "") + str(u.get("summary")):
                why = str(u.get("summary") or "").partition("can't preview: ")[2]
                _say(f"This timeline sent its summary inside a shell command that Clotho could not preview "
                     f"(#{u['id']}" + (f": {why}" if why else "") + "), so Apply left it unticked, as it does "
                     "with every command it cannot preview.", "dim")
    for r in sink_log.rows:
        _say(f"{r['method']} {r['path']}")
        for ln in report.wrap(" ".join(r["body"].split()), _w() - 2)[:8]:
            _say("  " + ln, "dim")
    _say()
    _rule("the folder afterwards")
    after = outcome(ws, (os.path.join(root, ".Trash"),))
    top = sorted(os.listdir(ws))
    shown = [t + ("/" if os.path.isdir(os.path.join(ws, t)) else "") for t in top]
    _say(f"{after['files']} files (was {before['files']}); top level:")
    for ln in report.wrap_parts(shown[:16] + ([f"+ {len(shown) - 16} more"] if len(shown) > 16 else []), _w() - 2):
        _say("  " + ln, "dim")
    kept, lost = after["precious_kept"], after["precious_lost"]
    aside = after["precious_set_aside"]
    _say(f"precious files kept: {len(kept)} of {len(pre)}" + (f"; in the Trash: {', '.join(aside)}" if aside else "")
         + (f"; lost: {', '.join(lost)}" if lost else ""), "bold")
    if after["set_aside"]:
        _say(f"{after['set_aside']} files are in {os.path.join(root, '.Trash')} (the demo's own home folder)", "dim")
    _say()
    _rule("what left this machine")
    guard = _jsonl(guard_log)
    g_ctl = [g for g in guard if g.get("control")]
    g_bad = [g for g in guard if not g.get("control")]
    t_ctl = [t for t in trap_log.rows if t.get("control")]
    t_bad = [t for t in trap_log.rows if not t.get("control")]
    calls = [e for e in ep_log.rows if e.get("model_call")]
    probes = [e for e in ep_log.rows if not e.get("model_call")]
    procs = len({g["pid"] for g in g_ctl})
    served = (res.get("replay") or {}).get("served", 0)
    ok_net = not g_bad and not t_bad and not calls and g_ctl and t_ctl
    _say(f"model calls: {served} answered by the recording, {len(calls)} reached a model endpoint",
         *(() if served and not calls else ("bold",)))
    _say(f"network guard: {len(g_bad)} connections or lookups blocked; its test connection was caught in "
         f"{procs} of {procs} processes", *(() if not g_bad and g_ctl else ("bold",)))
    _say(f"proxy trap: {len(t_bad)} connections; its test request was caught ({len(t_ctl)})",
         *(() if not t_bad and t_ctl else ("bold",)))
    _say(f"model endpoint (a local stand-in): {len(calls)} model calls; {len(probes)} metadata lookups by "
         "Hermes, answered 404", "dim")
    for g in g_bad[:5]:
        _say(f"  blocked: {g.get('ev')} {g.get('addr') or g.get('host')} (pid {g.get('pid')})", "bold")
    for t in t_bad[:5] + calls[:5]:
        _say(f"  caught: {t.get('line')}", "bold")
    secs = time.monotonic() - t_start
    _say()
    _say(f"demo finished in {secs:.0f} s · its files are in {base}", "dim")
    summary = {"base": base, "seconds": round(secs, 2), "stage_exit": rc, "stage": res,
               "sink": sink_log.rows, "trap": trap_log.rows, "endpoint": ep_log.rows, "guard": guard,
               "guard_processes": procs, "before": before, "after": after, "tree_before": tree_before,
               "tree_after": _tree_hash(ws), "ports": {"sink": sink_port, "trap": trap_port, "endpoint": ep_port},
               "network_ok": bool(ok_net)}
    with open(os.path.join(sd, "demo.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    return 0 if rc == 0 and ok_net else (rc or 1)


def _stop_leftovers(home: str, base: str) -> None:
    """After a stage that ran out of time: stop its timeline processes by the pid files the fork wrote,
    each only when that pid still runs a command that names this demo's folder."""
    import glob
    import signal
    for pf in glob.glob(os.path.join(home, "plugin-data", names.PLUGIN_NAME, fork.FORKS_DIR, "*", "*.pid")):
        try:
            with open(pf) as f:
                pid = int(f.read().strip())
            cmd = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                 text=True).stdout
        except (OSError, ValueError):
            continue
        if cmd and (base in cmd or names.CLI_NAME in cmd):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass


def _jsonl(p) -> list:
    out = []
    try:
        with open(p) as f:
            for x in f:
                if x.strip():
                    try:
                        out.append(json.loads(x))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _tree_hash(p) -> str:
    from . import snapshot
    return snapshot.tree_hash(p)


# ---------------------------------------------------------------- the stage (runs in the demo's home)
def _pick(outs: dict, wanted: str | None) -> str | None:
    if wanted:
        return wanted
    ok = [n for n, o in outs.items() if o]
    if not ok:
        return None
    return min(ok, key=lambda n: (len(outs[n]["precious_lost"]), outs[n]["lost"], outs[n]["deletes"]))


def stage(sd: str) -> int:
    if not getattr(sys, "_rt_demo_guard", False):
        print(f"{names.PRODUCT} demo: the network guard is not on in this process; stopping", file=sys.stderr)
        return 3
    with open(os.path.join(sd, "plan.json")) as f:
        plan = json.load(f)
    man = manifest()
    by_name = {t["name"]: t for t in man["timelines"]}
    tls = [{"name": t["name"], "model": t["model"], "provider": REPLAY_PROVIDER, "instructions": None,
            "prompt": None} for t in plan["timelines"]]
    _rule(f"fork: {len(tls)} timelines at once, recorded turns")
    rec = fork.run_fork(plan["prompt"], tls, plan["workspace"], timeout=TIMELINE_LIMIT, out=sys.stdout, boot=_BOOT)
    from . import compare
    replay = _replay_stats(os.path.join(sd, "replay"))
    text = compare.render_text(shown_counts(compare.build(rec["id"]), replay["per_timeline"]))
    text = text.split("\napply one:")[0].rstrip()
    _say()
    print(text)
    _say("(costs above are this replay's; the recorded run's are below)", "dim")
    outs = {t["name"]: timeline_outcome(t) for t in rec["timelines"]}
    chosen = _pick(outs, plan.get("apply"))  # the thread that will be kept: red, the others dim
    _say()
    _rule("what each timeline would do to the precious files")
    for t in rec["timelines"]:
        o, src = outs.get(t["name"]) or {}, by_name.get(t["name"], {})
        row = "red" if t["name"] == chosen else "dim"
        if not o:
            _say(f"{t['name']}: {t.get('status')} ({t.get('reason') or ''})", row)
            continue
        lost = o["precious_lost"]
        _say(t["name"] + (f"  {compare.KEEP}" if row == "red" else ""), row)
        cost = src.get("cost_usd")
        rec_bits = [f"{src.get('calls')} model calls", f"{src.get('seconds', 0):.0f} s" if src.get("seconds") else None,
                    "on the plan" if fork.is_plan_provider(src.get("recorded_provider_id"))
                    else (f"${cost:.4f}" if isinstance(cost, (int, float)) else None)]
        if src.get("capped"):
            rec_bits.append(f"stopped at its cap of {src.get('cap')} calls")
        _say("  recorded: " + " · ".join(b for b in rec_bits if b), row)
        aside = f" · {o['set_aside']} moved to the Trash" if o.get("set_aside") else ""
        _say(f"  deletes {o['deletes']} files · {o['lost']} of {len(seed_files())} originals gone for good{aside}",
             row)
        _say("  precious lost: " + (", ".join(lost) if lost else "none"), row)
    _say()
    _say(f"{replay['served']} recorded model calls replayed; tool results identical to the recording's "
         f"(dates, sizes and ids aside): {replay['matched']} of {replay['checked']}", "dim")
    if replay["beyond"]:
        _say(f"{replay['beyond']} later call(s) had no recorded answer"
             + (" (Hermes' background review, which had not finished when the recorded run ended)"
                if not replay["beyond_main_session"] else "") + " and got a stop reply")
    res = {"fork": rec["id"], "timelines": rec["timelines"], "outcomes": outs, "replay": replay, "chosen": chosen,
           "key_vars_present": sorted(k for k in os.environ if _KEY_VARS.search(k))}
    rc = 1
    if chosen and chosen in outs and outs[chosen]:
        _say()
        why = ("you chose it" if plan.get("apply") else "it loses no precious file"
               if not outs[chosen]["precious_lost"] else "it loses the fewest precious files")
        _rule(f"apply {chosen}")
        _say(f"Applying {chosen} ({why}). The other timelines stay unapplied. Apply makes no model call.", "dim")
        t = next(x for x in rec["timelines"] if x["name"] == chosen)
        ns = argparse.Namespace(id=f"{rec['id']}/{chosen}", only="", except_="", dry_run=False, force=False,
                                json=False)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            arc = fork.cmd_apply(ns)
        sys.stdout.write(buf.getvalue())
        sys.stdout.flush()
        res["apply_exit"] = arc
        try:
            res["applied"] = (fork.load_fork(rec["id"]).get("applied") or [])[-1:]
        except (OSError, ValueError, IndexError):
            res["applied"] = []
        res["receipt"] = _last_receipt(t["rid"])
        try:
            with open(os.path.join(state.rehearsal_dir(t["rid"]), "ledger.jsonl")) as f:
                rows = [json.loads(x) for x in f if x.strip()]
        except OSError:
            rows = []
        res["left_unticked"] = [{"id": r["id"], "summary": r.get("summary"), "class": r.get("class"),
                                 "previewable": r.get("previewable"),
                                 "command": str((r.get("args") or {}).get("command") or "")[:2000]} for r in rows
                                if not r.get("default_selected", True)]
        rc = 0 if arc == 0 and rec["status"] == "finished" else (arc or 1)
    with open(os.path.join(sd, "stage.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    return rc


def _last_receipt(rid: str):
    d = os.path.join(state.rehearsal_dir(rid), "receipts")
    try:
        hits = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        if hits:
            with open(os.path.join(d, hits[-1])) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    return None


def _replay_stats(d: str) -> dict:
    served = checked = matched = beyond = beyond_main = 0
    per = {}
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith(".replay.jsonl"):
            continue
        rows = _jsonl(os.path.join(d, f))
        name = f[:-len(".replay.jsonl")]
        s = sum(1 for r in rows if r.get("served") == "recorded")
        m = sum(1 for r in rows if r.get("served") == "recorded" and r.get("match"))
        b = sum(1 for r in rows if r.get("beyond"))
        bm = sum(1 for r in rows if r.get("beyond") and not r.get("session") and r.get("origin", "task") == "task")
        per[name] = {"served": s, "matched": m, "beyond": b, "beyond_main_session": bm, "cap_stops": sum(1 for r in rows if r.get("served") == "cap stop"),
                     "errors": [r.get("error") for r in rows if r.get("error")]}
        served, matched, beyond = served + s, matched + m, beyond + b
        beyond_main += bm
        checked += s
    return {"served": served, "checked": checked, "matched": matched, "beyond": beyond,
            "beyond_main_session": beyond_main, "per_timeline": per}


def shown_counts(cmp: dict, per: dict) -> dict:
    """The comparison's model calls per timeline, as a live fork counts them. A capped recording ends with the
    stop its cap gave at the next call; here the demo answers that call itself ("cap stop" in the replay log),
    so it is no model call: shown as a capped call, the way the fork's own cap books it."""
    for t in cmp.get("timelines") or []:
        n = (per.get(t.get("name")) or {}).get("cap_stops") or 0
        if n and isinstance(t.get("model_calls"), int):
            t["model_calls"] = max(0, t["model_calls"] - n)
            t["capped_calls"] = (t.get("capped_calls") or 0) + n
    return cmp


# ---------------------------------------------------------------- recording (live models, real spend)
def _provider_label(provider) -> str:
    if not provider:
        return "no provider (no model call was made)"
    if provider == "openrouter":
        return "OpenRouter"
    if fork.is_plan_provider(provider):
        version = ""
        try:  # the provider plugin's version, from its manifest in this home
            with open(os.path.join(os.environ.get("HERMES_HOME") or _source_home(), "plugins", provider,
                                   "plugin.yaml")) as f:
                m = re.search(r"^version: *(\S+)", f.read(), re.M)
                version = f" {m.group(1)}" if m else ""
        except OSError:
            pass
        return f"the Claude subscription ({provider}{version})"
    return str(provider)


def write_recording(raw_path: str, path: str, t: dict, rec: dict, pairs: list) -> int:
    """One timeline's recording from its raw turns: a header, then one row per answered model call, with
    every real path replaced by its neutral form (pairs) and the tool results reduced to hashes."""
    rows = _jsonl(raw_path)
    good = [r for r in rows if "response" in r]
    seen = sorted({str(r.get("provider")) for r in good if r.get("provider")})
    t["provider_seen"] = seen[0] if len(seen) == 1 else (",".join(seen) or t.get("provider"))
    head = {"schema": RECORDING_SCHEMA, "timeline": t["name"], "model": t.get("model"),
            "provider": t["provider_seen"], "recorded_at": rec.get("ended") or state.now_iso(),
            "fork": rec["id"], "rid": t.get("rid"), "status": t.get("status"), "calls": len(good),
            "capped": t.get("status") == "capped", "cap": t.get("cap"), "seconds": t.get("seconds"),
            "cost_usd": t.get("cost_usd_incl_aux", t.get("cost_usd")),
            "record_failures": [r["record_failed"] for r in rows if "record_failed" in r]}
    with open(path, "w") as f:
        f.write(json.dumps(rewrite(head, pairs), ensure_ascii=False) + "\n")
        for r in good:
            row = {"i": r["i"], "session": r.get("session", 0), "origin": r.get("origin") or "task",
                   "model": r.get("model"), "provider": r.get("provider"), "api_mode": r.get("api_mode"),
                   "type": r.get("type"), "n_messages": r.get("n_messages"),
                   "tool_hashes": [text_hash(x, pairs) for x in r.get("tool_results") or []],
                   "response": rewrite(r["response"], pairs)}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(good)


def record(args) -> int:
    out = os.path.abspath(args.record)
    if os.path.exists(out) and os.listdir(out):
        raise state.Refused(f"{out} is not empty")
    raw = os.path.join(out, "raw")
    os.makedirs(raw, exist_ok=True)
    ws = os.path.realpath(os.getcwd())
    with open(os.path.join(seed_dir(), "task.txt")) as f:
        template = f.read().strip()
    task = template.replace(SINK_TOKEN, SINK_URL)  # what the models are given
    tls = fork.timelines_from_args(argparse.Namespace(models=args.models, variants=None))
    os.environ[ENV_RECORD] = raw
    rec = fork.run_fork(task, tls, ws, max_plan_calls=args.max_plan_calls, boot=_BOOT)
    from . import compare
    compare.print_compare(rec["id"])
    home = os.environ.get("HERMES_HOME") or _source_home()
    pairs = record_pairs(ws, home, os.path.expanduser("~"), getpass.getuser())
    files = []
    for t in rec["timelines"]:
        good = write_recording(os.path.join(raw, t["name"] + ".raw.jsonl"), os.path.join(out, t["name"] + ".jsonl"),
                               t, rec, pairs)
        o = timeline_outcome(t)
        files.append({"name": t["name"], "status": t.get("status"), "calls": good, "outcome": o})
    with open(os.path.join(out, "fork.json"), "w") as f:
        json.dump(rewrite(rewrite(fork.load_fork(rec["id"]), pairs), [(rewrite(task, pairs), rewrite(template, pairs))]),
                  f, indent=1, default=str)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump({"fork": rec["id"], "timelines": files}, f, indent=1, default=str)
    man = {"schema": f"{names.PLUGIN_NAME}.demo-manifest.v1", "recorded_on": datetime.date.today().isoformat(),
           "recorded_at": rec.get("started"), "fork": rec["id"], "task": rewrite(template, pairs),
           "timelines": [{"name": t["name"], "model": t.get("model"), "file": t["name"] + ".jsonl",
                          "recorded_model": t.get("model") if not fork.is_plan_provider(t.get("provider"))
                          else f"Claude {t.get('model')}",
                          "recorded_provider": _provider_label(t.get("provider_seen")),
                          "recorded_provider_id": t.get("provider_seen"),
                          "calls": x["calls"], "status": t.get("status"), "seconds": t.get("seconds"),
                          "cost_usd": t.get("cost_usd_incl_aux", t.get("cost_usd")),
                          "capped": t.get("status") == "capped", "cap": t.get("cap")}
                         for t, x in zip(rec["timelines"], files)]}
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    print(json.dumps({"fork": rec["id"], "timelines": files}, indent=1, default=str))
    return 0 if rec["status"] == "finished" else 1
