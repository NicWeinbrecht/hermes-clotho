"""Run one shell command inside the rehearsal's work clone under macOS sandbox-exec (clotho.sb).

No network, writes only inside the clone, a private tmp and (optional) the home overlay, secrets
unreadable, effect-only binaries (osascript, open, crontab, launchctl, ...) refused.

What was blocked is gathered from several sources, because the kernel's sandbox log alone is lossy
(it drops or delays some reports for short-lived processes):
  * the unified log, read through one long-lived ``log stream`` per rehearsal (``log show`` as the
    fallback), tagged with a per-command message and closed by a sentinel denial;
  * a Python audit hook (sitecustomize on PYTHONPATH) that records every write outside the allowed
    roots and every socket use by Python processes, whether the program reports the error or not;
  * the command's output and exit code ("Operation not permitted", connection errors).
Anything the sources cannot confirm either way is reported as "may have been blocked" (fail closed).

With a home overlay, HOME points at a mirror of the real home: top-level files are copy-on-write
clones, folders are links to the real ones (reads see the real tree, writes through them stay
blocked), and ~/.Trash is an empty folder of its own. Writes that land in the mirror come back as
``outside_changes`` with the real path they stand for.

After the command returns, its process group and every other process still running in this
rehearsal's sandbox (found by sandbox state plus a per-rehearsal marker, so a child that called
setsid is found too) are killed and reaped before the post-run manifest is taken.
"""
from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

try:
    from . import names, snapshot
except ImportError:  # imported as a top-level module by the unit tests
    import names
    import snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, names.SANDBOX_PROFILE)
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
LOG = "/usr/bin/log"
STREAM_TIMEOUT = "6h"     # a stream left behind by a crashed parent ends by itself
STREAM_IDLE_S = 15 * 60   # streams of rehearsals unused this long are closed
GRACE_S = 0.1             # extra wait after the sentinel for reports that arrive just behind it

STDERR_NET = re.compile(r"Could not resolve host|nodename nor servname|Couldn't connect to server|"
                        r"Failed to connect to|Network is unreachable|getaddrinfo|ENOTFOUND|EAI_AGAIN|"
                        r"Connection refused|ECONNREFUSED|Name or service not known|Temporary failure in name resolution")
STDERR_EPERM = re.compile(r"Operation not permitted|PermissionError: \[Errno 1\]|EPERM|\[Errno 1\]")

NETWORK_TOOLS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "ftp", "gh", "http",
                 "https", "httpie", "xh", "mail", "sendmail", "mutt", "aws", "gcloud", "az", "kubectl", "docker"}
EFFECT_BINARIES = {"osascript", "open", "crontab", "launchctl", "at", "batch", "defaults", "security",
                   "shortcuts", "systemsetup"}
NETWORK_SUBCOMMANDS = {("git", "push"), ("git", "pull"), ("git", "fetch"), ("git", "clone"), ("git", "ls-remote"),
                       ("npm", "install"), ("npm", "publish"), ("npm", "i"), ("pnpm", "install"), ("yarn", "install"),
                       ("pip", "install"), ("pip3", "install"), ("brew", "install"), ("brew", "upgrade"),
                       ("brew", "update"), ("docker", "push"), ("docker", "pull")}
SHELL_OPERATORS = {";", "&&", "||", "|", "&", ">", ">>", "<", "<<", "<<<", "|&", "(", ")", "`", "$("}
SEGMENT_BREAKS = {";", "&&", "||", "|", "&", "|&", "(", ")"}

# curl / wget options whose file arguments the request preview does not show
CURL_SHORT_ARG = set("AbcCdDeEFHKmoPQrtTuUwxXyYz")
CURL_LONG_ARG = {
    "--url", "--output", "--output-dir", "--config", "--cookie", "--cookie-jar", "--data", "--data-raw",
    "--data-binary", "--data-urlencode", "--data-ascii", "--json", "--form", "--form-string", "--header",
    "--proxy-header", "--request", "--user", "--user-agent", "--referer", "--max-time", "--connect-timeout",
    "--write-out", "--upload-file", "--retry", "--retry-delay", "--retry-max-time", "--dump-header", "--trace",
    "--trace-ascii", "--stderr", "--libcurl", "--etag-save", "--etag-compare", "--hsts", "--alt-svc", "--proxy",
    "--proxy-user", "--resolve", "--connect-to", "--cacert", "--capath", "--cert", "--key", "--cert-type",
    "--key-type", "--pass", "--limit-rate", "--max-filesize", "--range", "--continue-at", "--time-cond",
    "--interface", "--local-port", "--dns-servers", "--ciphers", "--oauth2-bearer", "--unix-socket",
    "--abstract-unix-socket", "--variable", "--url-query", "--aws-sigv4", "--keepalive-time", "--speed-limit",
    "--speed-time", "--max-redirs", "--quote", "--telnet-option", "--mail-from", "--mail-rcpt", "--mail-auth",
    "--create-file-mode", "--netrc-file", "--noproxy", "--preproxy", "--proto", "--proto-redir",
    "--proto-default", "--rate", "--doh-url", "--request-target", "--login-options", "--parallel-max",
    "--socks4", "--socks4a", "--socks5", "--socks5-hostname", "--happy-eyeballs-timeout-ms", "--expand-data",
    "--expand-url", "--expand-header", "--service-name", "--sasl-authzid", "--tls-max", "--pinnedpubkey"}
CURL_WRITE_OPTS = {"-D": "--dump-header", "-c": "--cookie-jar"}
CURL_WRITE_LONG = {"--dump-header", "--cookie-jar", "--trace", "--trace-ascii", "--stderr", "--libcurl",
                   "--etag-save", "--hsts", "--alt-svc"}
CURL_READ_LONG = {"--config", "--etag-compare", "--hsts", "--alt-svc", "--netrc-file", "--upload-file"}
WGET_SHORT_ARG = set("OoaPietTwQUlARDIXB")


def available() -> tuple[bool, str]:
    """(usable, reason). Preview needs macOS, sandbox-exec and the profile."""
    if sys.platform != "darwin":
        return False, "shell preview needs macOS sandbox-exec (not available on this platform)"
    if not os.path.exists(SANDBOX_EXEC):
        return False, "sandbox-exec not found"
    if not os.path.exists(PROFILE):
        return False, f"sandbox profile {names.SANDBOX_PROFILE} missing"
    return True, "ok"


def _tokens(command: str):
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None


def _segments(toks):
    """Simple commands of a token list, split at ; && || | & ( )."""
    seg = []
    for t in toks:
        if t in SEGMENT_BREAKS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def _program(seg):
    """(basename, args) of a simple command, skipping VAR=x prefixes, env, sudo, command, exec, nohup, time."""
    i = 0
    while i < len(seg):
        w = seg[i]
        b = os.path.basename(w)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or b in ("env", "sudo", "command", "exec", "nohup", "time"):
            i += 1
            continue
        return b, seg[i + 1:]
    return None, []


def _url_name(url: str) -> str:
    name = os.path.basename(urlparse(url).path or "")
    return name or "<name from the URL>"


def _curl_files(args):
    """(writes, reads) of one curl invocation: files the request preview would not show."""
    writes, reads, outs, urls = [], [], [], []
    outdir, remote_all = None, False

    def opt(o, v):
        nonlocal outdir, remote_all
        if o in ("-o", "--output"):
            outs.append(("o", v))
        elif o in ("-O", "--remote-name"):
            outs.append(("O", None))
        elif o == "--remote-name-all":
            remote_all = True
        elif o in ("-J", "--remote-header-name"):
            outs.append(("J", None))
        elif o == "--output-dir":
            outdir = v
        elif o in ("-K", "--config"):
            if v != "-":
                reads.append(v)
        elif o in ("-b", "--cookie"):
            if v and "=" not in v:
                reads.append(v)
        elif o in ("-F", "--form"):
            m = re.search(r"=[@<]([^;]+)", v or "")
            if m:
                reads.append(m.group(1))
        elif o in ("-T", "--upload-file"):
            if v and v != "-":
                reads.append(v)
        elif o in CURL_WRITE_OPTS or o in CURL_WRITE_LONG:
            if v and v != "-":
                writes.append(v)
            if o in CURL_READ_LONG and v and v != "-":
                reads.append(v)
        elif o in CURL_READ_LONG and v and v != "-":
            reads.append(v)
        elif o == "--url" and v:
            urls.append(v)

    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "--":
            urls.extend(args[i + 1:])
            break
        if a.startswith("--"):
            if a in CURL_LONG_ARG:
                opt(a, nxt)
                i += 2
            else:
                opt(a, None)
                i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            j, consumed = 1, 1
            while j < len(a):
                ch = a[j]
                if ch in CURL_SHORT_ARG:
                    rest = a[j + 1:]
                    if rest:
                        opt("-" + ch, rest)
                    else:
                        opt("-" + ch, nxt)
                        consumed = 2
                    break
                opt("-" + ch, None)
                j += 1
            i += consumed
            continue
        urls.append(a)
        i += 1
    if remote_all:
        while len([o for o in outs if o[0] in "oO"]) < len(urls):
            outs.append(("O", None))
    k = 0
    for kind, v in outs:
        if kind == "J":
            writes.append("<file name chosen by the server>")
            continue
        url = urls[k] if k < len(urls) else ""
        k += 1
        name = v if kind == "o" else _url_name(url)
        if name in ("-", "/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        writes.append(os.path.join(outdir, name) if outdir and not os.path.isabs(name) else name)
    return writes, reads


def _wget_files(args):
    writes, reads, urls = [], [], []
    doc, prefix, spider, recursive = None, None, False, False

    def opt(o, v):
        nonlocal doc, prefix, spider, recursive
        if o in ("-O", "--output-document"):
            doc = v
        elif o in ("-o", "--output-file", "-a", "--append-output", "--save-cookies"):
            if v and v != "-":
                writes.append(v)
        elif o in ("-P", "--directory-prefix"):
            prefix = v
        elif o in ("-i", "--input-file", "--post-file", "--body-file", "--load-cookies", "--config"):
            if v and v != "-":
                reads.append(v)
        elif o == "--spider":
            spider = True
        elif o in ("-r", "--recursive", "-m", "--mirror"):
            recursive = True

    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                opt(k, v)
            else:
                opt(a, None)
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            j, consumed = 1, 1
            while j < len(a):
                ch = a[j]
                if ch in WGET_SHORT_ARG:
                    rest = a[j + 1:]
                    opt("-" + ch, rest if rest else nxt)
                    consumed = 1 if rest else 2
                    break
                opt("-" + ch, None)
                j += 1
            i += consumed
            continue
        urls.append(a)
        i += 1
    if not spider:
        if recursive:
            writes.append("<files of a recursive download>")
        elif doc is not None:
            if doc not in ("-", "/dev/null"):
                writes.append(doc)
        else:
            for u in urls:
                n = os.path.basename(urlparse(u).path or "") or "index.html"
                writes.append(os.path.join(prefix, n) if prefix else n)
    return writes, reads


def http_files(command: str):
    """(writes, reads): files a curl/wget in ``command`` would write, or read without showing them."""
    toks = _tokens(command) or []
    writes, reads = [], []
    for seg in _segments(toks):
        prog, args = _program(seg)
        if prog == "curl":
            w, r = _curl_files(args)
        elif prog == "wget":
            w, r = _wget_files(args)
        else:
            continue
        writes += [x for x in w if x not in writes]
        reads += [x for x in r if x not in reads]
    return writes, reads


def static_scan(command: str) -> dict:
    """Cheap labels from the command text. The sandbox is the enforcement; this only labels."""
    toks = _tokens(command)
    flags = []
    words = [os.path.basename(t) for t in (toks or command.split())]
    if toks is None:
        flags.append("unparsed")
    if toks is not None and "&" in toks:
        flags.append("background")
    if any(w in ("nohup", "setsid", "disown") for w in words):
        flags.append("background")
    if any(w in NETWORK_TOOLS for w in words) or re.search(r"https?://", command):
        flags.append("network")
    for i in range(len(words) - 1):
        if (words[i], words[i + 1]) in NETWORK_SUBCOMMANDS:
            flags.append("network")
    if any(w in EFFECT_BINARIES for w in words):
        flags.append("effect-binary")
    if "sudo" in words:
        flags.append("sudo")
    writes, reads = http_files(command) if toks is not None else ([], [])
    return {"flags": sorted(set(flags)), "writes": writes, "reads": reads}


def parse_http(command: str):
    """A single plain curl/wget invocation -> {method, url, headers, body, writes, reads}; anything else -> None.
    ``writes`` / ``reads`` list files the request would save or send without the preview showing them."""
    toks = _tokens(command)
    if not toks or any(t in SHELL_OPERATORS for t in toks) or "$(" in command or "`" in command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    prog = os.path.basename(argv[0])
    method, url, headers, body = None, None, [], None
    if prog == "curl":
        i = 1
        while i < len(argv):
            a = argv[i]
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if a in ("-X", "--request"):
                method = (nxt or "").upper(); i += 2; continue
            if a.startswith("-X") and len(a) > 2:
                method = a[2:].upper(); i += 1; continue
            if a in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json"):
                body = nxt if body is None else body + "&" + (nxt or ""); i += 2
                if a == "--json":
                    headers.append("Content-Type: application/json")
                continue
            if a in ("-F", "--form"):
                body = nxt if body is None else body + "&" + (nxt or ""); i += 2; continue
            if a in ("-H", "--header"):
                headers.append(nxt or ""); i += 2; continue
            if a == "--url":
                url = nxt; i += 2; continue
            if a in ("-T", "--upload-file"):
                body = f"<upload of {nxt}>"
                method = method or "PUT"
                i += 2; continue
            if a in CURL_LONG_ARG or (len(a) == 2 and a[0] == "-" and a[1] in CURL_SHORT_ARG):
                i += 2; continue
            if a.startswith("-"):
                i += 1; continue
            url = url or a
            i += 1
        if not url:
            return None
        method = method or ("POST" if body is not None else "GET")
    elif prog == "wget":
        for i, a in enumerate(argv[1:], 1):
            if a.startswith("--post-data="):
                body, method = a.split("=", 1)[1], method or "POST"
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
            elif a.startswith("--header="):
                headers.append(a.split("=", 1)[1])
            elif not a.startswith("-"):
                url = url or a
        if not url:
            return None
        method = method or "GET"
    else:
        return None
    body_file = None
    if body is not None and body.startswith("@"):
        body_file = body[1:]
        body = f"<contents of {body_file}>"
    writes, reads = http_files(command)
    return {"method": method, "url": url, "headers": headers, "body": body, "body_file": body_file,
            "writes": writes, "reads": reads}


def _sb_argv(work, tmp, tag, argv, orig=None, home_mirror=None):
    cmd = [SANDBOX_EXEC, "-D", f"CLONE={work}", "-D", f"TMP={tmp}",
           "-D", f"HOME={os.path.realpath(os.path.expanduser('~'))}", "-D", f"TAG={tag}"]
    if orig:
        cmd += ["-D", f"ORIG={orig}"]
    if home_mirror:
        cmd += ["-D", f"HOMEMIRROR={home_mirror}"]
    return cmd + ["-f", PROFILE] + argv


# ---------------------------------------------------------------- unified log readback
_VIOLATION = re.compile(r"Sandbox: (\S+)\((\d+)\) deny\(\d+\) (\S+) ?([^\n]*)")


def _parse_violations(msgs, skip=None):
    out = []
    for m in msgs:
        if skip and skip in m:
            continue
        mm = _VIOLATION.search(m)
        if mm:
            out.append({"process": mm.group(1), "pid": int(mm.group(2)),
                        "operation": mm.group(3), "target": mm.group(4).strip()})
    return out


def _log_rows(tag, start):
    pred = f'sender == "Sandbox" AND eventMessage CONTAINS "{tag}"'
    r = subprocess.run([LOG, "show", "--start", start, "--style", "ndjson", "--predicate", pred],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"log show exited {r.returncode}")
    rows = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def read_violations(tag, start, sentinel_argv, sentinel_name, budget_s=8.0):
    """Fallback reader: issue one denied sentinel write with the same tag, then poll ``log show``
    until it shows up. None when the log does not answer within the budget."""
    subprocess.run(sentinel_argv, capture_output=True)
    t0 = time.monotonic()
    while True:
        try:
            msgs = [r.get("eventMessage", "") for r in _log_rows(tag, start)]
        except Exception:
            return None
        msgs = [m for m in msgs if _tag_of(m, tag.rsplit("-", 1)[0] + "-") == tag]
        if any(sentinel_name in m for m in msgs):
            return _parse_violations(msgs, skip=sentinel_name)
        if time.monotonic() - t0 > budget_s:
            return None
        time.sleep(0.25)


def _tag_of(msg: str, prefix: str):
    for line in reversed(msg.split("\n")):
        line = line.strip()
        if line.startswith(prefix):
            return line
    return None


class _Stream:
    """One ``log stream`` for every command of a rehearsal (all tags share ``prefix``)."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.msgs: dict = {}
        self.taken: dict = {}
        self.losses = 0
        self.dead = False
        self.cond = threading.Condition()
        self.last_used = time.monotonic()
        pred = f'sender == "Sandbox" AND eventMessage CONTAINS "{prefix}"'
        self.proc = subprocess.Popen([LOG, "stream", "--style", "ndjson", "--timeout", STREAM_TIMEOUT,
                                      "--predicate", pred], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read, name=f"{names.PLUGIN_NAME}-log-stream", daemon=True).start()

    def _read(self):
        try:
            for raw in self.proc.stdout:
                if not raw.startswith(b"{"):
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                msg = d.get("eventMessage") or ""
                if "loss" in str(d.get("eventType") or "").lower():
                    with self.cond:
                        self.losses += 1
                        self.cond.notify_all()
                    continue
                tag = _tag_of(msg, self.prefix)
                if tag:
                    with self.cond:
                        self.msgs.setdefault(tag, []).append(msg)
                        self.cond.notify_all()
        finally:
            with self.cond:
                self.dead = True
                self.cond.notify_all()

    def alive(self) -> bool:
        return not self.dead and self.proc.poll() is None

    def wait_for(self, tag: str, needle: str, budget_s: float) -> bool:
        end = time.monotonic() + budget_s
        with self.cond:
            while True:
                if any(needle in m for m in self.msgs.get(tag, ())):
                    return True
                left = end - time.monotonic()
                if left <= 0 or self.dead:
                    return False
                self.cond.wait(left)

    def take(self, tag: str) -> list:
        with self.cond:
            got = list(self.msgs.get(tag, ()))
            self.taken[tag] = len(got)
            self.last_used = time.monotonic()
            return got

    def late(self) -> list:
        """Reports that arrived for a command after its result was returned."""
        with self.cond:
            out = []
            for tag, n in self.taken.items():
                for v in _parse_violations(self.msgs.get(tag, [])[n:], skip=names.PLUGIN_NAME + "-sentinel-"):
                    out.append({"tag": tag, **v})
            return out

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass


_streams: dict = {}
_streams_lock = threading.Lock()


def _stream_for(prefix: str, work: str, tmp: str):
    """The live stream for this rehearsal, started (and checked with a ready sentinel) on first use."""
    with _streams_lock:
        now = time.monotonic()
        for p, s in list(_streams.items()):
            if p != prefix and now - s.last_used > STREAM_IDLE_S:
                s.close()
                _streams.pop(p, None)
        s = _streams.get(prefix)
        if s is not None and s.alive():
            return s
        if s is not None:
            _streams.pop(prefix, None)
        try:
            s = _Stream(prefix)
        except OSError:
            return None
        ready = f"{names.PLUGIN_NAME}-sentinel-ready-{time.time_ns()}"
        tag = prefix + "ready"
        for _ in range(3):  # the stream may still be connecting when the first sentinel fires
            _sentinel(work, tmp, tag, ready)
            if s.wait_for(tag, ready, 1.5):
                _streams[prefix] = s
                return s
            if not s.alive():
                break
        s.close()
        return None


def close_streams():
    with _streams_lock:
        for s in _streams.values():
            s.close()
        _streams.clear()


def late_violations(tag: str) -> list:
    """Sandbox reports for earlier commands of the rehearsal ``tag`` belongs to that arrived after their
    results were returned (each with its command's ``tag``). Callers report them as blocked actions."""
    s = _streams.get(tag.rsplit("-", 1)[0] + "-")
    return s.late() if s else []


atexit.register(close_streams)


def _sentinel(work, tmp, tag, name):
    target = os.path.join(os.path.dirname(tmp), name)
    subprocess.run(_sb_argv(work, tmp, tag, ["/bin/sh", "-c", f": > {shlex.quote(target)}"]), capture_output=True)


# ---------------------------------------------------------------- python audit hook
_AUDIT_HOOK = r'''
# Written by the rehearsal runner: records what the sandbox will refuse, so a refusal the program
# swallows still reaches the result. Never changes behaviour; chains to the next sitecustomize.
import os, sys
def _install():
    log = os.environ.get(%(log)r)
    if not log or not hasattr(sys, "addaudithook"):
        return
    allow = tuple(p for p in os.environ.get(%(allow)r, "").split(os.pathsep) if p)
    deny_read = tuple(p for p in os.environ.get(%(deny)r, "").split(os.pathsep) if p)
    state = {"busy": False}
    W = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    def path_of(p):
        if isinstance(p, int) or p is None:
            return None
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if not isinstance(p, str):
            p = os.fspath(p)
        if not os.path.isabs(p):
            p = os.path.join(os.getcwd(), p)
        return os.path.realpath(p)
    def inside(p):
        return p == "/dev/null" or p.startswith("/dev/") or any(p == a or p.startswith(a + "/") for a in allow)
    def emit(op, target):
        state["busy"] = True
        try:
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (repr((os.getpid(), op, target)) + "\n").encode("utf-8", "replace"))
            finally:
                os.close(fd)
        except Exception:
            pass
        finally:
            state["busy"] = False
    def write(op, p):
        p = path_of(p)
        if p and not inside(p):
            emit(op, p)
    WRITE_ARG0 = {"os.remove": "file-write-unlink", "os.rmdir": "file-write-unlink", "shutil.rmtree": "file-write-unlink",
                  "os.mkdir": "file-write-create", "os.chmod": "file-write-mode", "os.chown": "file-write-owner",
                  "os.utime": "file-write-times", "os.truncate": "file-write-data", "os.chflags": "file-write-flags",
                  "os.lchflags": "file-write-flags", "os.setxattr": "file-write-xattr", "os.removexattr": "file-write-xattr"}
    WRITE_ARG1 = {"os.link": "file-write-create", "os.symlink": "file-write-create", "shutil.copyfile": "file-write-create",
                  "shutil.copytree": "file-write-create", "shutil.move": "file-write-create"}
    NET = {"socket.connect": "network-outbound", "socket.sendto": "network-outbound",
           "socket.sendmsg": "network-outbound", "socket.bind": "network-bind"}
    def hook(event, args):
        if state["busy"]:
            return
        try:
            if event == "open":
                p, mode, flags = (tuple(args) + (None, None, None))[:3]
                w = (isinstance(flags, int) and flags & W) or (isinstance(mode, str) and any(c in mode for c in "wax+"))
                if w:
                    write("file-write-data", p)
                elif deny_read:
                    q = path_of(p)
                    if q and (any(q == d or q.startswith(d + "/") for d in deny_read)
                              or os.path.basename(q) == ".env" or os.path.basename(q).startswith(".env.")):
                        emit("file-read-data", q)
            elif event in WRITE_ARG0:
                write(WRITE_ARG0[event], args[0])
            elif event in WRITE_ARG1:
                write(WRITE_ARG1[event], args[1])
            elif event == "os.rename":
                write("file-write-unlink", args[0])
                write("file-write-create", args[1])
            elif event in NET:
                addr = args[1] if len(args) > 1 else None
                if event in ("socket.sendto", "socket.sendmsg") and addr is None:
                    return
                emit(NET[event], str(addr))
            elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
                host = args[0] if args else None
                if isinstance(host, bytes):
                    host = host.decode("ascii", "replace")
                if host and not all(c in "0123456789.:abcdefABCDEF" for c in str(host)):
                    emit("network-outbound", "dns:" + str(host))
        except Exception:
            pass
    sys.addaudithook(hook)
try:
    _install()
except Exception:
    pass
def _chain():
    import importlib.machinery, importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for d in sys.path:
        if os.path.abspath(d or ".") == here:
            continue
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", [d or "."])
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["sitecustomize"] = mod
            spec.loader.exec_module(mod)
            return
_chain()
'''


def _audit_env(tmp, tag, allow, deny_read, env):
    hook_dir = os.path.join(tmp, "pyaudit")
    os.makedirs(hook_dir, exist_ok=True)
    text = _AUDIT_HOOK % {"log": names.env("AUDIT_LOG"), "allow": names.env("AUDIT_ALLOW"),
                          "deny": names.env("AUDIT_DENY_READ")}
    with open(os.path.join(hook_dir, "sitecustomize.py"), "w") as f:  # rewritten each time: tmp is writable
        f.write(text)
    log = os.path.join(tmp, f"audit-{tag}.log")
    if os.path.exists(log):
        os.replace(log, log + ".prev")
    env[names.env("AUDIT_LOG")] = log
    env[names.env("AUDIT_ALLOW")] = os.pathsep.join(allow)
    env[names.env("AUDIT_DENY_READ")] = os.pathsep.join(deny_read)
    env["PYTHONPATH"] = hook_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return log


def _read_audit(log):
    out = []
    try:
        with open(log) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    import ast
    seen = set()
    for line in lines:
        try:
            pid, op, target = ast.literal_eval(line)
        except Exception:
            continue
        if (op, target) in seen:
            continue
        seen.add((op, target))
        out.append({"process": "python", "pid": int(pid), "operation": op, "target": str(target), "source": "audit"})
    return out


_HOME = os.path.realpath(os.path.expanduser("~"))
_NOISE_WRITE_PREFIXES = (_HOME + "/Library/Caches/", _HOME + "/.npm/_logs/")
_SECRET_SUBPATHS = (".ssh", ".gnupg", ".aws", ".config/gh", ".docker", ".kube", "Library/Keychains")
_SECRET_LITERALS = (".netrc", ".git-credentials", ".hermes/.env", ".hermes/auth.json")


def _deny_read_roots(orig, home=_HOME):
    roots = [os.path.join(home, p) for p in _SECRET_SUBPATHS + _SECRET_LITERALS]
    if orig:
        roots.append(os.path.realpath(orig))
    return roots


def classify_violations(violations, tag_marker):
    """Derive blocked_* flags. Substring matching, so forms such as
    ``forbidden-link-priv<file-write*>`` (a hard link to an outside file) count as writes."""
    res = {"blocked_network": False, "blocked_write": False, "blocked_read": False, "blocked_exec": [],
           "blocked_ipc": [], "noise": []}
    for v in violations:
        op, target, proc = v["operation"], v["target"], v["process"]
        if "file-write" in op and target.startswith(_NOISE_WRITE_PREFIXES):
            res["noise"].append(v); continue
        if op == "mach-lookup" and "launchservicesd" in target and proc == "node":
            res["noise"].append(v); continue
        if "network" in op:
            res["blocked_network"] = True
        if "file-write" in op or op.startswith("forbidden-link"):
            res["blocked_write"] = True
        if "file-read" in op:
            res["blocked_read"] = True
        if "process-exec" in op:
            res["blocked_exec"].append(target)
        if op in ("mach-lookup", "appleevent-send") or "appleevent" in op:
            res["blocked_ipc"].append(target)
    res["blocked_exec"] = sorted(set(res["blocked_exec"]))
    res["blocked_ipc"] = sorted(set(res["blocked_ipc"]))
    return res


_EPERM_PATH = re.compile(r"(/[^\s:'\",]+)")


def _output_signals(stderr: str, flags: dict) -> list:
    """Fail-closed reasons from the output when no source confirmed a block."""
    out = []
    lines = [ln for ln in stderr.splitlines() if STDERR_EPERM.search(ln)]
    real = []
    for ln in lines:
        paths = [p for p in _EPERM_PATH.findall(ln) if p.count("/") > 1]
        if paths and all(os.path.realpath(p).startswith(_NOISE_WRITE_PREFIXES) or p.startswith(_NOISE_WRITE_PREFIXES)
                         for p in paths):
            continue
        real.append(ln)
    confirmed = (flags.get("blocked_network") or flags.get("blocked_write") or flags.get("blocked_read")
                 or flags.get("blocked_exec") or flags.get("blocked_ipc"))
    if real and not confirmed:
        out.append('may have been blocked by the sandbox ("Operation not permitted" in its output; '
                   "the sandbox log did not confirm what)")
    return out


# ---------------------------------------------------------------- processes that outlive the command
class _Procs:
    """Find this rehearsal's sandboxed processes: sandboxed (sandbox_check) and carrying its marker
    in the environment, or working inside its folders. Nothing else is ever signalled."""

    def __init__(self):
        self.ok = False
        try:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            self.check = lib.sandbox_check
            self.check.restype = ctypes.c_int
            self.check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            self.listpids = lib.proc_listallpids
            self.listpids.restype = ctypes.c_int
            self.listpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.pidinfo = lib.proc_pidinfo
            self.pidinfo.restype = ctypes.c_int
            self.pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            self.sysctl = lib.sysctl
            self.sysctl.restype = ctypes.c_int
            self.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
            self.argmax = self._argmax()
            self.buf = ctypes.create_string_buffer(self.argmax)
            self.cbuf = ctypes.create_string_buffer(2 * (152 + 1024))  # struct proc_vnodepathinfo
            self.ok = True
        except (OSError, AttributeError):
            pass

    def _argmax(self):
        mib = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
        v = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(v))
        if self.sysctl(mib, 2, ctypes.byref(v), ctypes.byref(size), None, 0) != 0:
            return 1 << 18
        return v.value

    def pids(self):
        n = self.listpids(None, 0)
        if n <= 0:
            return []
        buf = (ctypes.c_int * (n + 64))()
        n = self.listpids(buf, ctypes.sizeof(buf))
        return [buf[i] for i in range(max(n, 0)) if buf[i] > 1]

    def sandboxed(self, pid) -> bool:
        return self.check(pid, None, 0) == 1

    def environ(self, pid):
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        buf = self.buf
        size = ctypes.c_size_t(self.argmax)
        if self.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[:size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack("i", raw[:4])[0]
        parts = raw[4:].split(b"\0")
        i = 1  # exec path
        while i < len(parts) and parts[i] == b"":
            i += 1
        i += argc
        return [p.decode("utf-8", "replace") for p in parts[i:] if p]

    def cwd(self, pid):
        buf, size = self.cbuf, ctypes.sizeof(self.cbuf)
        if self.pidinfo(pid, 9, 0, buf, size) <= 0:  # PROC_PIDVNODEPATHINFO
            return None
        return buf.raw[152:152 + 1024].split(b"\0", 1)[0].decode("utf-8", "replace")

    def find(self, marker: str, roots) -> list:
        if not self.ok:
            return []
        me, found = os.getpid(), []
        for pid in self.pids():
            if pid == me or not self.sandboxed(pid):
                continue
            env = self.environ(pid)
            if env is not None and marker in env:
                found.append(pid)
                continue
            c = self.cwd(pid)
            if c and any(c == r or c.startswith(r + "/") for r in roots):
                found.append(pid)
        return found


_procs = None


def _sweep(marker: str, roots) -> list:
    """SIGKILL every process of this rehearsal's sandbox still alive; returns their pids."""
    global _procs
    if _procs is None:
        _procs = _Procs()
    killed = []
    for _ in range(5):
        found = _procs.find(marker, roots)
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)
    return sorted(set(killed))


# ---------------------------------------------------------------- home overlay
_SECRET_NAMES = {".ssh", ".gnupg", ".aws", ".docker", ".kube", ".netrc", ".git-credentials", ".hermes"}
_CLONE_MAX = 64 << 20


def _home_mirror(overlay: str, real_home: str) -> str:
    """overlay/home: HOME for sandboxed commands. Built once per rehearsal."""
    root = os.path.join(overlay, "home")
    done = os.path.join(overlay, "built")
    if os.path.exists(done):
        return root
    os.makedirs(root, exist_ok=True)
    try:
        entries = os.listdir(real_home)
    except OSError:
        entries = []
    for name in entries:
        src, dst = os.path.join(real_home, name), os.path.join(root, name)
        if os.path.lexists(dst):
            continue
        try:
            st = os.lstat(src)
            if name == ".Trash":
                os.mkdir(dst)  # a Trash of its own: moves into it are kept and reported
            elif (stat.S_ISREG(st.st_mode) and st.st_size <= _CLONE_MAX and name not in _SECRET_NAMES
                  and not (name == ".env" or name.startswith(".env."))):
                snapshot.clonefile(src, dst)
            else:
                os.symlink(src, dst)  # folders and secrets: reads go to the real path, writes stay blocked
        except OSError:
            try:
                os.symlink(src, dst)
            except OSError:
                pass
    if not os.path.lexists(os.path.join(root, ".Trash")):
        os.mkdir(os.path.join(root, ".Trash"))
    with open(done, "w") as f:
        f.write(real_home + "\n")
    return root


def _outside_changes(changes, root, snap, real_home):
    out = []
    for c in changes:
        rel = c["path"]
        row = {"path": os.path.join(real_home, rel), "abs_path": os.path.join(real_home, rel), "rel": rel,
               "change": c["change"], "kind": c.get("kind", "file"), "overlay_path": os.path.join(root, rel),
               "before_path": os.path.join(snap, c.get("from") or rel) if c["change"] != "created" else None,
               "before_sha256": c.get("before_sha256"), "after_sha256": c.get("after_sha256")}
        if c.get("from"):
            row["from"] = os.path.join(real_home, c["from"])
        for k in ("before_mode", "after_mode"):
            if c.get(k):
                row[k] = c[k]
        out.append(row)
    return out


def restore_home(res: dict) -> None:
    """Put the home overlay back to its state before the command of ``res`` (if the caller discards it)."""
    h = res.get("home_overlay") or {}
    root, snap = h.get("root"), h.get("snapshot")
    if not root or not snap or not os.path.isdir(snap):
        return
    move_aside(root, os.path.join(os.path.dirname(root), "discarded"))
    snapshot.clonefile(snap, root)
    if res.get("outside_changes"):
        res["outside_changes_discarded"] = (res.get("outside_changes_discarded") or []) + res["outside_changes"]
        res["outside_changes"] = []


# ---------------------------------------------------------------- run
def run(work, cwd, command, tmp, tag, timeout=60, orig=None, shell="/bin/bash", use_log=True, extra_env=None,
        home_overlay=None, real_home=None):
    """Run ``command`` with cwd ``cwd`` (inside ``work``) under the sandbox. Returns a result dict.

    ``tag`` is ``<rehearsal prefix>-<n>``: commands that share the prefix share one log stream and one
    process marker. ``home_overlay`` (a folder of the rehearsal, outside the clone) turns on the home
    mirror; the result then carries ``outside_changes``. When the command cannot be previewed, the mirror
    is put back as it was and the changes move to ``outside_changes_discarded``."""
    t0 = time.monotonic()
    os.makedirs(tmp, exist_ok=True)
    tmp = os.path.realpath(tmp)
    prefix = tag.rsplit("-", 1)[0] + "-"
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    shim = os.path.join(bindir, "mktemp")
    if not os.path.exists(shim):
        with open(shim, "w") as f:
            f.write('#!/bin/sh\ncase "$*" in */*) exec /usr/bin/mktemp "$@";; esac\n'
                    'exec /usr/bin/mktemp -p "$TMPDIR" "$@"\n')
        os.chmod(shim, 0o755)
    real_home = os.path.realpath(real_home or os.path.expanduser("~"))
    mirror = snap = None
    if home_overlay:
        os.makedirs(home_overlay, exist_ok=True)
        home_overlay = os.path.realpath(home_overlay)
        mirror = _home_mirror(home_overlay, real_home)
        snaps = os.path.join(home_overlay, "snaps")
        os.makedirs(snaps, exist_ok=True)
        snap = os.path.join(snaps, f"{len(os.listdir(snaps)) + 1}-{time.time_ns()}")
        snapshot.clonefile(mirror, snap)
        pre = snapshot.stat_manifest(mirror)
    cwd_file = os.path.join(tmp, "last_cwd")
    if os.path.exists(cwd_file):
        os.replace(cwd_file, cwd_file + ".prev")
    wrapped = f"trap 'builtin pwd -P > {shlex.quote(cwd_file)} 2>/dev/null' EXIT\n{command}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENROUTER", "ANTHROPIC", "OPENAI"))}
    env.update(GIT_OPTIONAL_LOCKS="0", TMPDIR=tmp + "/", TMPPREFIX=os.path.join(tmp, "zsh"),
               PATH=bindir + ":" + env.get("PATH", "/usr/bin:/bin"), PWD=cwd,
               PYTHONDONTWRITEBYTECODE="1")
    if mirror:
        env["HOME"] = mirror
    env[names.SANDBOX_ENV] = "1"
    marker = f"{names.env('SANDBOX_ID')}={prefix}"
    env[names.env("SANDBOX_ID")] = prefix
    roots = [os.path.realpath(work), tmp] + ([mirror] if mirror else [])
    audit_log = _audit_env(tmp, tag, roots, _deny_read_roots(orig, real_home), env)
    if extra_env:
        env.update(extra_env)
    stream = _stream_for(prefix, work, tmp) if use_log else None
    log_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
    losses0 = stream.losses if stream else 0
    argv = _sb_argv(work, tmp, tag, [shell, "-c", wrapped], orig, mirror)
    timed_out = False
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
    # Kill and reap anything the command left behind before the post-run manifest: first its process
    # group, then any process of this rehearsal's sandbox that left the group (setsid, double fork).
    survivors = False
    try:
        os.killpg(p.pid, 0)
        survivors = True
        os.killpg(p.pid, signal.SIGKILL)
        time.sleep(0.05)
    except (ProcessLookupError, PermissionError):
        pass
    swept = _sweep(marker, roots)
    if swept:
        survivors = True
    command_s = time.monotonic() - t0
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if mirror:  # the command saw the mirror as HOME; its output names the home the agent knows
        swap = re.compile(re.escape(mirror) + r"(?=$|/|[^\w.-])")
        stdout, stderr = swap.sub(real_home, stdout), swap.sub(real_home, stderr)
    violations, detection, reader = None, "stderr-heuristic", None
    maybe = []
    if use_log:
        sname = f"{names.PLUGIN_NAME}-sentinel-{int(time.time() * 1000)}"
        if stream is not None:
            _sentinel(work, tmp, tag, sname)
            if stream.wait_for(tag, sname, 3.0):
                time.sleep(GRACE_S)
                violations = _parse_violations(stream.take(tag), skip=sname)
                reader = "stream"
                if stream.losses != losses0:
                    maybe.append("may have been blocked: the sandbox log reported dropped messages")
        if violations is None:
            sentinel = _sb_argv(work, tmp, tag, ["/bin/sh", "-c",
                                                 f": > {shlex.quote(os.path.dirname(tmp))}/{sname}"])
            violations = read_violations(tag, log_start, sentinel, sname)
            reader = "show" if violations is not None else None
        if violations is not None:
            detection = "unified-log"
        else:
            maybe.append("may have been blocked: the sandbox log did not answer, so a blocked action "
                         "cannot be ruled out")
    audited = _read_audit(audit_log)
    merged = list(violations or []) + audited
    flags = classify_violations(merged, tag)
    if not flags["blocked_network"] and STDERR_NET.search(stderr):
        flags["blocked_network"] = True
    http = parse_http(command)
    if http and not flags["blocked_network"]:
        prog = os.path.basename(shlex.split(command)[0])
        if (prog == "curl" and p.returncode in (5, 6, 7)) or (prog == "wget" and p.returncode == 4):
            flags["blocked_network"] = True
    maybe += _output_signals(stderr, flags)
    new_cwd = None
    try:
        with open(cwd_file) as f:
            new_cwd = f.read().strip() or None
    except OSError:
        pass
    res = {"stdout": stdout, "stderr": stderr, "exit_code": p.returncode, "timed_out": timed_out,
           "survivors": survivors, "swept": swept, "violations": merged, "detection": detection,
           "log_reader": reader, "audit_events": len(audited), "maybe_blocked": maybe, **flags,
           "cwd_after": new_cwd, "outside_changes": [], "outside_changes_discarded": [],
           "home_overlay": {"root": mirror, "snapshot": snap} if mirror else None}
    if mirror:
        post = snapshot.stat_manifest(mirror)
        res["outside_changes"] = _outside_changes(snapshot.diff_effects(snap, mirror, pre, post), mirror, snap,
                                                  real_home)
        if res["outside_changes"] and reasons_cant_preview(res, static_scan(command)):
            restore_home(res)
    res.update(seconds=round(time.monotonic() - t0, 3), command_s=round(command_s, 3))
    return res


def reasons_cant_preview(res: dict, scan: dict) -> list:
    r = []
    if res.get("blocked_network"):
        r.append("needs the network")
    if res.get("blocked_write"):
        r.append("writes outside the workspace")
    if res.get("blocked_read"):
        r.append("reads a protected path (secrets or the live workspace)")
    if res.get("blocked_exec"):
        r.append("runs " + ", ".join(os.path.basename(x) for x in res["blocked_exec"]))
    if res.get("blocked_ipc"):
        r.append("talks to a system service (" + ", ".join(res["blocked_ipc"]) + ")")
    if res.get("survivors") or "background" in scan.get("flags", []):
        r.append("starts a background process")
    if res.get("timed_out"):
        r.append("timed out in the sandbox")
    for m in res.get("maybe_blocked") or []:
        if m not in r:
            r.append(m)
    if scan.get("writes"):
        r.append("would save " + ", ".join(scan["writes"]) + " (not shown in the request preview)")
    if scan.get("reads"):
        r.append("would read " + ", ".join(scan["reads"]) + " (not shown in the request preview)")
    return r


def move_aside(path: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path) + "-" + str(int(time.time() * 1000)))
    shutil.move(path, dest)
    return dest
