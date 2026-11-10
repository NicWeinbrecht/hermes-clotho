"""Evaluation runner: every task with the plugin on, then Apply, then the same task with the plugin off.

    python eval/run.py --home <scratch Hermes home> [--driver scripted|openrouter] [--tasks t01_notes,...]
                       [--reps N] [--model M] [--provider P] [--off all|control|none] [--port-base 19030]

Usually started through eval/run.sh. Nothing leaves the machine except the model calls of the
openrouter driver: the sinks, the scripted endpoint and the proxy trap listen on 127.0.0.1, and
Apply runs inside a sandbox profile that denies every non-localhost connection.
Results: eval/results/<timestamp>/results.json (schema in eval/README.md) plus a printed table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import smtplib
import subprocess
import sys
import time
import urllib.request
from email.message import EmailMessage

import lib
import tasks as T
from lib import names

SCHEMA = "clotho.eval.v1"
RID = re.compile(r"(" + re.escape(names.RID_PREFIX) + r"\d{8}-\d{6}-[0-9a-f]{4})")
STATE_DIRS = ("memories", "skills", "cron")
# Hermes' cron ticker rewrites these on its own in any Hermes process; they are listed, not counted as effects.
# dispatch_held.json: an idle-exit gateway's ticker stamps the times of the ticks it skipped while draining.
HOUSEKEEPING = {"cron/ticker_heartbeat", "cron/ticker_last_success", "cron/.tick.lock", "cron/.jobs.lock",
                "cron/dispatch_held.json"}
CONTROL_IP = "192.0.2.1"  # RFC 5737 documentation address: nothing answers there even if a control leaks


def split_state_changes(paths):
    """(counted as escapes, Hermes housekeeping) for changed paths under the home's state folders."""
    counted = [p for p in paths if p not in HOUSEKEEPING]
    return counted, [p for p in paths if p in HOUSEKEEPING]


class Ctx:
    def __init__(self, run, tag):
        self.SINK, self.SMTP, self.TAG = run.sink_port, run.smtp_port, tag
        self.SENTINEL = None

    def child(self, base):
        return f"{base}_{self.TAG}_child"


class Run:
    def __init__(self, a):
        self.a = a
        self.home = os.path.realpath(a.home)
        if self.home == os.path.realpath(os.path.expanduser("~/.hermes")):
            sys.exit("refusing: --home is ~/.hermes; use a scratch home")
        if not os.path.isdir(self.home):
            sys.exit(f"no such home: {self.home}")
        self.ts = time.strftime("%Y%m%d-%H%M%S")
        self.sink_port, self.smtp_port, self.stub_port, self.trap_port = (a.port_base + i for i in range(4))
        self.work = os.path.join(a.work or os.path.join(os.path.dirname(self.home), "eval-runs"), self.ts)
        for d in ("ws", "scripts", "logs", "sentinel", "trash"):
            os.makedirs(os.path.join(self.work, d), exist_ok=True)
        self.out_dir = os.path.join(a.out or os.path.join(lib.EVAL, "results"), self.ts)
        os.makedirs(self.out_dir, exist_ok=True)
        self.L = lambda *p: os.path.join(self.work, "logs", *p)
        self.sink_log, self.smtp_log, self.stub_log, self.trap_log = (
            self.L("sink.jsonl"), self.L("smtp.jsonl"), self.L("stub.jsonl"), self.L("trap.jsonl"))
        self.hermes = lib.hermes_cmd(self.home)
        self.n = 0
        self.servers = []
        self.cost = 0.0
        self.scripted = a.driver == "scripted"
        self.model = a.model or ("scripted" if self.scripted else "deepseek/deepseek-v4-flash")
        self.provider = a.provider or ("evalstub" if self.scripted else "openrouter")

    # ------------------------------------------------------------------------------------ servers + controls
    def start(self):
        S = lambda name, script, port, log: lib.Server(name, [lib.PY, "-I", os.path.join(lib.EVAL, "instruments", script),
                                                              str(port), log], port, self.L())
        srv = [S("sink", "http_sink.py", self.sink_port, self.sink_log),
               S("smtp", "smtp_sink.py", self.smtp_port, self.smtp_log),
               S("trap", "http_sink.py", self.trap_port, self.trap_log)]
        if self.scripted:
            srv.append(lib.Server("stub", [lib.PY, "-I", os.path.join(lib.REPO, "tests", "fixtures", "scripted_provider.py"),
                                           str(self.stub_port), os.path.join(self.work, "scripts"), self.stub_log],
                                  self.stub_port, self.L()))
        for s in srv:
            s.start()
            self.servers.append(s)

    def stop(self):
        for s in self.servers:
            try:
                s.stop()
            except Exception:
                pass

    def controls(self) -> dict:
        """Positive controls: every instrument must see the thing it is there to see."""
        c = {}
        n = lib.count_lines(self.sink_log)
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.sink_port}/selftest", data=b"x",
                                                      method="POST"), timeout=5).read()
        c["sink_logs_a_post"] = lib.count_lines(self.sink_log) == n + 1
        n = lib.count_lines(self.smtp_log)
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "selftest@eval.test", "selftest@eval.test", "selftest"
        m.set_content("selftest")
        with smtplib.SMTP("127.0.0.1", self.smtp_port, timeout=5) as s:
            s.send_message(m)
        c["smtp_logs_a_message"] = lib.count_lines(self.smtp_log) == n + 1
        n = lib.count_lines(self.trap_log)
        subprocess.run([lib.PY, "-I", "-c", "import urllib.request\ntry: urllib.request.urlopen("
                        "'https://model-api.invalid/v1/models', timeout=5)\nexcept Exception: pass"],
                       env={**os.environ, **self.proxy_env()}, capture_output=True, timeout=20)
        c["trap_sees_a_proxied_call"] = lib.count_lines(self.trap_log) == n + 1
        tag = f"{names.PLUGIN_NAME}-evalctl-{secrets.token_hex(3)}"
        t0 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
        p = subprocess.Popen(self.nonet(tag) + ["/usr/bin/curl", "-s", "-m", "3", f"http://{CONTROL_IP}:9/"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = p.wait()
        c["sandbox_blocks_internet"] = rc != 0
        den = self.denials(tag, t0)
        c["sandbox_logs_the_denial"] = any(f"({p.pid})" in d for d in den)
        if self.scripted:
            c["scripted_endpoint_up"] = not lib.port_free(self.stub_port)
        return c

    def nonet(self, tag):
        return ["/usr/bin/sandbox-exec", "-D", f"TAG={tag}", "-f", os.path.join(lib.EVAL, "instruments", "nonet.sb")]

    def proxy_env(self):
        t = f"http://127.0.0.1:{self.trap_port}"
        return dict(HTTPS_PROXY=t, HTTP_PROXY=t, https_proxy=t, http_proxy=t, ALL_PROXY=t,
                    NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")

    def denials(self, tag, start) -> list:
        r = subprocess.run(["/usr/bin/log", "show", "--start", start, "--style", "ndjson", "--predicate",
                            f'sender == "Sandbox" AND eventMessage CONTAINS "{tag}"'],
                           capture_output=True, text=True, timeout=120)
        return [ln for ln in r.stdout.splitlines() if tag in ln]

    # ------------------------------------------------------------------------------------ hermes
    def env(self, sentinel, **extra):
        e = dict(os.environ)
        e.pop("FORCE_COLOR", None)
        e.update(HERMES_HOME=self.home, PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1",
                 EVALKIT_SINK_PORT=str(self.sink_port), EVALKIT_SMTP_PORT=str(self.smtp_port),
                 EVALKIT_SENTINEL=sentinel)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def hx(self, args, cwd, label, sentinel, wrap=None, timeout=None, **extra):
        self.n += 1
        return lib.run_logged((wrap or []) + self.hermes + args, cwd, self.env(sentinel, **extra),
                              self.L(f"{self.n:03d}_{label}"), timeout or self.a.timeout)

    def write_scripts(self, t, c):
        if not self.scripted:
            return ""
        main = f"{t['id']}_{c.TAG}"
        for name, steps in {main: t["script"](c), **t["children"](c)}.items():
            with open(os.path.join(self.work, "scripts", name + ".json"), "w") as f:
                json.dump({"steps": steps, "final": "Done."}, f)
        return f"[script:{main}] "

    def make_ws(self, name, files):
        d = os.path.join(self.work, "ws", name)
        os.makedirs(d)
        for rel, text in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        return os.path.realpath(d)

    def make_sentinel(self, name):
        d = os.path.join(self.work, "sentinel", name)
        os.makedirs(d)
        with open(os.path.join(d, "keep.txt"), "w") as f:
            f.write("keep\n")
        return os.path.realpath(d)

    def state_snapshot(self):
        return {f"{d}/{e[0]}": e[1:] for d in STATE_DIRS for e in lib.entries(os.path.join(self.home, d))}

    @staticmethod
    def state_changes(a, b):
        return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))

    def counts(self):
        return {"sink": lib.count_lines(self.sink_log), "smtp": lib.count_lines(self.smtp_log),
                "stub": lib.count_lines(self.stub_log), "trap": lib.count_lines(self.trap_log)}

    def new(self, which, since):
        log = {"sink": self.sink_log, "smtp": self.smtp_log, "trap": self.trap_log, "stub": self.stub_log}[which]
        return lib.jsonl(log)[since:]

    # ------------------------------------------------------------------------------------ one task
    def task(self, t, rep):
        tag = secrets.token_hex(3)
        c = Ctx(self, tag)
        label = f"{t['id']}-r{rep}"
        res = {"task": t["id"], "title": t["title"], "rep": rep, "tag": tag, "covers": t["covers"],
               "apply_variant": t["apply"], "on": None, "drift": None, "apply": None, "off": None,
               "overhead": None, "verdict": None}
        failures = []
        c.SENTINEL = self.make_sentinel(label + "-on")
        prompt = self.write_scripts(t, c) + t["prompt"](c)
        files = t["files"](c)
        ws = self.make_ws(label + "-on", files)
        before = {"ws": lib.tree_hash(ws), "sentinel": lib.tree_hash(c.SENTINEL), "state": self.state_snapshot()}
        before_map = lib.content_map(ws)
        n0 = self.counts()
        ids0 = lib.rehearsal_ids(self.home)
        run = self.hx([names.CLI_NAME, "run", prompt, "-m", self.model, "--provider", self.provider,
                       "--workspace", ws, "--no-diffs"], ws, label + "_on", c.SENTINEL)
        time.sleep(self.a.settle)  # a detached child gets time to try a late write
        m = RID.search(run["stderr"] + run["stdout"])
        rid = m.group(1) if m else next(iter(lib.rehearsal_ids(self.home) - ids0), None)
        on = {"exit_code": run["rc"], "timed_out": run["timed_out"], "rid": rid, "wall_s": run["wall_s"],
              "log": run["log"]}
        esc = {"sink_new": len(self.new("sink", n0["sink"])), "smtp_new": len(self.new("smtp", n0["smtp"])),
               "workspace_changed": lib.tree_hash(ws) != before["ws"],
               "sentinel_changed": lib.tree_hash(c.SENTINEL) != before["sentinel"],
               "hermes_state_changed": [], "hermes_housekeeping": []}
        esc["hermes_state_changed"], esc["hermes_housekeeping"] = split_state_changes(
            self.state_changes(before["state"], self.state_snapshot()))
        esc["sink_paths"] = [r["path"] for r in self.new("sink", n0["sink"])]
        esc["total"] = (esc["sink_new"] + esc["smtp_new"] + int(esc["workspace_changed"]) +
                        int(esc["sentinel_changed"]) + len(esc["hermes_state_changed"]))
        on["escapes"] = esc
        if esc["total"]:
            failures.append(f"escape during the rehearsal: {json.dumps(esc)}")
        rows, meta, calls = [], {}, []
        if rid:
            rdir = os.path.join(lib.data_root(self.home), rid)
            rows = lib.jsonl(os.path.join(rdir, "ledger.jsonl"))
            calls = lib.jsonl(os.path.join(rdir, "calls.jsonl"))
            try:
                with open(os.path.join(rdir, "meta.json")) as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {}
        else:
            failures.append("no rehearsal id found in the output")
        on.update(model_calls=meta.get("model_calls"), tool_calls=meta.get("tool_calls"),
                  cost_usd=meta.get("cost_usd"), seconds_inside=meta.get("seconds"), status=meta.get("status"))
        self.cost += float(meta.get("cost_usd") or 0)
        if run["rc"] != 0:
            failures.append(f"rehearsal exit {run['rc']}")
        expected = t["expect"](c)
        sc = lib.score(expected, rows, lib.parent_task_id(rows))
        sc["attempted_tools"] = sorted({x.get("tool") for x in calls if x.get("where") == "middleware"})
        sc["blocked_rows"] = sum(1 for r in rows if r.get("class") == "blocked")
        on["ledger"] = sc
        if sc["missed"]:
            failures.append(f"missed {len(sc['missed'])} expected effect(s)")
        if sc["extra"]:
            failures.append(f"{len(sc['extra'])} extra ledger row(s)")
        for mt in sc["matches"]:
            if mt["how"] == "bridge-wrapped":
                failures.append(f"effect #{mt['row_ids'][0]} is recorded under the bridge name tool_call, "
                                f"not {expected[mt['expect']].get('tool')}")
        res["on"] = on

        if rid and t.get("drift") and not self.a.no_drift:
            res["drift"] = self.drift(t, c, ws, rid, files)
            if not res["drift"]["pass"]:
                failures.append("drift case did not stop Apply cleanly")
        if rid:
            res["apply"] = self.apply(t, c, ws, rid, rows, expected, sc, before_map)
            if not res["apply"]["pass"]:
                failures.extend("apply: " + f for f in res["apply"]["failures"])

        if self.a.off == "all" or (self.a.off == "control" and t["off_control"]):
            res["off"] = self.off(t, rep)
            if t["off_control"] and not res["off"]["escape_visible"]:
                failures.append("OFF control: the instruments saw no effect with the plugin off")
            if res["off"]["new_rehearsal_dirs"]:
                failures.append("OFF run created a rehearsal folder")
            if res["off"]["wall_s"]:
                res["overhead"] = {"on_wall_s": on["wall_s"], "off_wall_s": res["off"]["wall_s"],
                                   "ratio": round(on["wall_s"] / res["off"]["wall_s"], 2),
                                   "delta_s": round(on["wall_s"] - res["off"]["wall_s"], 2)}
        res["verdict"] = {"pass": not failures, "failures": failures}
        return res

    def drift(self, t, c, ws, rid, files):
        d = t["drift"]
        path = d.get("edit") or d.get("create")
        p = os.path.join(ws, path)
        if "edit" in d:
            with open(p, "a") as f:
                f.write("edited by hand after the rehearsal\n")
        else:
            with open(p, "w") as f:
                f.write("made by hand after the rehearsal\n")
        pre = lib.tree_hash(ws)
        n0 = self.counts()
        a = self.hx([names.CLI_NAME, "apply", rid, "--json"], ws, f"{t['id']}_drift_apply", c.SENTINEL)
        rc = self._json(a["stdout"])
        out = {"kind": "edit" if "edit" in d else "create", "path": path, "exit_code": a["rc"],
               "status": rc.get("status"), "drift_paths": [x.get("path") for x in rc.get("drift") or []],
               "workspace_unchanged": lib.tree_hash(ws) == pre, "sink_new": len(self.new("sink", n0["sink"])),
               "smtp_new": len(self.new("smtp", n0["smtp"])), "receipt_model_calls": rc.get("model_calls")}
        out["pass"] = (a["rc"] == 4 and out["status"] == "drift" and path in out["drift_paths"]
                       and out["workspace_unchanged"] and out["sink_new"] == 0 and out["smtp_new"] == 0)
        if "edit" in d:
            with open(p, "w") as f:
                f.write(files[path])
        else:
            shutil.move(p, os.path.join(self.work, "trash", f"{t['id']}-{c.TAG}-{os.path.basename(path)}"))
        return out

    @staticmethod
    def _json(text):
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return {}

    def apply(self, t, c, ws, rid, rows, expected, sc, before_map):
        fails = []
        except_ids = []
        if t["apply"] == "unticked":
            for mt in sc["matches"]:
                if expected[mt["expect"]].get("untick"):
                    except_ids.extend(mt["row_ids"])
        flags = ["--except", ",".join(map(str, sorted(except_ids)))] if except_ids else []
        if t["apply"] == "unticked" and not except_ids:
            fails.append("nothing to untick: the unticked items were not in the ledger")
        tag = f"{names.PLUGIN_NAME}-evalapply-{c.TAG}"
        t0 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
        n0 = self.counts()
        a = self.hx([names.CLI_NAME, "apply", rid, "--json", *flags], ws, f"{t['id']}_apply", c.SENTINEL,
                    wrap=self.nonet(tag), **self.proxy_env())
        rc = self._json(a["stdout"])
        applied = set(rc.get("applied_ids") or [])
        out = {"variant": t["apply"], "flags": flags, "exit_code": a["rc"], "status": rc.get("status"),
               "selected": rc.get("selected"), "applied": rc.get("applied"), "applied_ids": sorted(applied),
               "failed_ids": rc.get("failed_ids"), "not_appliable": rc.get("not_appliable"),
               "wall_s": a["wall_s"], "seconds_inside": rc.get("seconds"), "log": a["log"]}
        # model calls during Apply, four instruments
        den = self.denials(tag, t0)
        out["model_calls"] = {"receipt": rc.get("model_calls"), "stub_delta": len(self.new("stub", n0["stub"])),
                              "trap_delta": len(self.new("trap", n0["trap"])), "nonet_denials": len(den),
                              "denial_samples": den[:3]}
        mc = out["model_calls"]
        if mc["receipt"] != 0 or mc["stub_delta"] or mc["trap_delta"] or mc["nonet_denials"]:
            fails.append(f"model or internet calls during Apply: {json.dumps({k: mc[k] for k in mc if k != 'denial_samples'})}")
        if a["rc"] != 0 or rc.get("status") != "ok":
            fails.append(f"apply exit {a['rc']} status {rc.get('status')}")
        # what should have landed: expected items whose rows were applied
        def landed(i):
            return any(r in applied for mt in sc["matches"] if mt["expect"] == i for r in mt["row_ids"])
        want_sink = sorted((e["sink"]["method"], e["sink"]["path"]) for i, e in enumerate(expected)
                           if e.get("sink") and landed(i))
        got = self.new("sink", n0["sink"])
        got_sink = sorted((r["method"], r["path"]) for r in got)
        out["sink"] = {"expected": want_sink, "got": got_sink, "exact": want_sink == got_sink}
        if not out["sink"]["exact"]:
            fails.append(f"sink got {got_sink}, expected {want_sink}")
        for i, e in enumerate(expected):
            bf = (e.get("sink") or {}).get("body_from_file")
            if bf and landed(i):
                body = next((r["body"] for r in got if r["path"] == e["sink"]["path"]), None)
                live = open(os.path.join(ws, bf)).read() if os.path.exists(os.path.join(ws, bf)) else None
                out["sink"]["body_matches_file"] = body is not None and body == live
                if not out["sink"]["body_matches_file"]:
                    fails.append(f"POST body differs from {bf}")
        want_smtp = sorted((e["smtp"]["to"], e["smtp"].get("subject")) for i, e in enumerate(expected)
                           if e.get("smtp") and landed(i))
        got_smtp = sorted((",".join(m.get("rcpt_to") or []), m.get("subject")) for m in self.new("smtp", n0["smtp"]))
        out["smtp"] = {"expected": want_smtp, "got": got_smtp, "exact": want_smtp == got_smtp}
        if not out["smtp"]["exact"]:
            fails.append(f"smtp got {got_smtp}, expected {want_smtp}")
        # files: the workspace = its pre-run state plus exactly the applied file rows
        want = lib.expected_after(before_map, rows, applied)
        diff = lib.map_diff(want, lib.content_map(ws))
        out["files"] = {"match": not diff, "mismatches": diff[:20], "file_rows_applied":
                        sum(1 for r in rows if r.get("class") == "file" and r["id"] in applied),
                        "file_rows_unticked": sum(1 for r in rows if r.get("class") == "file" and r["id"] in except_ids)}
        if diff:
            fails.append(f"{len(diff)} file(s) differ from the ledger")
        checks = []
        for chk in t["post_apply"](c):
            kind = chk[0]
            if kind == "file_equals":
                p = os.path.join(ws, chk[1])
                ok = os.path.exists(p) and open(p).read() == chk[2]
            elif kind == "sentinel_contains":
                p = os.path.join(c.SENTINEL, chk[1])
                ok = os.path.exists(p) and chk[2] in open(p).read()
            elif kind == "home_contains":
                ok = _tree_contains(os.path.join(self.home, chk[1]), chk[2])
            else:
                ok = False
            checks.append({"check": list(chk), "ok": ok})
            if not ok:
                fails.append(f"post-apply check failed: {chk}")
        out["post_checks"] = checks
        out["failures"] = fails
        out["pass"] = not fails
        return out

    def off(self, t, rep):
        tag = secrets.token_hex(3)
        c = Ctx(self, tag)
        label = f"{t['id']}-r{rep}-off"
        c.SENTINEL = self.make_sentinel(label)
        prompt = self.write_scripts(t, c) + t["prompt"](c)
        ws = self.make_ws(label, t["files"](c))
        before = lib.tree_hash(ws)
        n0 = self.counts()
        ids0 = lib.rehearsal_ids(self.home)
        p = self.hx(["-z", prompt, "-m", self.model, "--provider", self.provider], ws, label, c.SENTINEL)
        expected = t["expect"](c)
        out = {"tag": tag, "exit_code": p["rc"], "wall_s": p["wall_s"], "sink_new": len(self.new("sink", n0["sink"])),
               "smtp_new": len(self.new("smtp", n0["smtp"])), "workspace_changed": lib.tree_hash(ws) != before,
               "sink_paths": [r["path"] for r in self.new("sink", n0["sink"])],
               "expected_sink": sum(1 for e in expected if e.get("sink")),
               "expected_smtp": sum(1 for e in expected if e.get("smtp")),
               "new_rehearsal_dirs": sorted(lib.rehearsal_ids(self.home) - ids0), "log": p["log"]}
        out["escape_visible"] = out["sink_new"] + out["smtp_new"] > 0
        out["control"] = t["off_control"]
        return out


def _tree_contains(root, text):
    for dirpath, _d, files in os.walk(root):
        if text in dirpath:
            return True
        for f in files:
            if text in f:
                return True
            try:
                with open(os.path.join(dirpath, f), errors="replace") as fh:
                    if text in fh.read(2_000_000):
                        return True
            except OSError:
                pass
    return False


def git_head():
    try:
        return subprocess.run(["git", "-C", lib.REPO, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip() or None
    except OSError:
        return None


def git_dirty():
    """Uncommitted plugin files: the home holds a copy of the working tree, which may differ from HEAD."""
    try:
        out = subprocess.run(["git", "-C", lib.REPO, "status", "--porcelain", "--", "*.py", "plugin.yaml", "*.sb"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    return [ln[3:] for ln in out.splitlines() if ln[3:] and not ln[3:].startswith(("eval/", "tests/"))]


def totals(results):
    on = [r["on"] for r in results if r["on"]]
    ap = [r["apply"] for r in results if r["apply"]]
    dr = [r["drift"] for r in results if r["drift"]]
    off = [r["off"] for r in results if r["off"]]
    ov = [r["overhead"] for r in results if r["overhead"]]
    s = lambda k: sum(o["ledger"][k] for o in on if o.get("ledger"))  # noqa: E731
    return {
        "task_runs": len(results), "passed": sum(1 for r in results if r["verdict"]["pass"]),
        "expected_effects": s("expected"), "captured": s("captured"), "covered_unpreviewed": s("covered_unpreviewed"),
        "missed": sum(len(o["ledger"]["missed"]) for o in on if o.get("ledger")),
        "extra": sum(len(o["ledger"]["extra"]) for o in on if o.get("ledger")),
        "escapes": sum(o["escapes"]["total"] for o in on),
        "applies": len(ap), "applies_ok": sum(1 for a in ap if a["pass"]),
        "apply_model_calls": sum((a["model_calls"]["receipt"] or 0) + a["model_calls"]["stub_delta"]
                                 + a["model_calls"]["trap_delta"] for a in ap),
        "apply_nonet_denials": sum(a["model_calls"]["nonet_denials"] for a in ap),
        "drift_cases": len(dr), "drift_stopped": sum(1 for d in dr if d["pass"]),
        "off_runs": len(off), "off_controls": sum(1 for o in off if o["control"]),
        "off_controls_visible": sum(1 for o in off if o["control"] and o["escape_visible"]),
        "median_overhead_ratio": sorted(o["ratio"] for o in ov)[len(ov) // 2] if ov else None,
    }


def table(results) -> str:
    hdr = ["task", "rep", "on", "esc", "capt/exp", "cov", "miss", "extra", "apply", "sink", "files", "mcalls",
           "drift", "off", "on_s", "off_s", "x", "PASS"]
    rows = [hdr]
    for r in results:
        on, ap, dr, off, ov = r["on"] or {}, r["apply"] or {}, r["drift"], r["off"], r["overhead"]
        lg = on.get("ledger") or {}
        mc = ap.get("model_calls") or {}
        rows.append([
            r["task"], str(r["rep"]), str(on.get("exit_code")), str((on.get("escapes") or {}).get("total")),
            f"{lg.get('captured')}/{lg.get('expected')}", str(lg.get("covered_unpreviewed")),
            str(len(lg.get("missed") or [])), str(len(lg.get("extra") or [])),
            f"{ap.get('status')}" + ("*" if ap.get("variant") == "unticked" else ""),
            "ok" if (ap.get("sink") or {}).get("exact") and (ap.get("smtp") or {}).get("exact") else ("-" if not ap else "NO"),
            "ok" if (ap.get("files") or {}).get("match") else ("-" if not ap else "NO"),
            "-" if not ap else str((mc.get("receipt") or 0) + mc.get("stub_delta", 0) + mc.get("trap_delta", 0)
                                   + mc.get("nonet_denials", 0)),
            "-" if not dr else ("stop" if dr["pass"] else "NO"),
            "-" if not off else (f"{off['sink_new'] + off['smtp_new']}hit" + ("!" if off["control"] else "")),
            f"{on.get('wall_s', 0):.1f}", "-" if not off else f"{off['wall_s']:.1f}",
            "-" if not ov else f"{ov['ratio']:.2f}", "yes" if r["verdict"]["pass"] else "NO"])
    w = [max(len(x[i]) for x in rows) for i in range(len(hdr))]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(row)) for row in rows]
    lines.insert(1, "  ".join("-" * x for x in w))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.environ.get("EVAL_HOME"), required=not os.environ.get("EVAL_HOME"))
    ap.add_argument("--driver", choices=("scripted", "openrouter"), default="scripted")
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all; 'real' = the real-model subset)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--off", choices=("all", "control", "none"), default="all",
                    help="run the same task with the plugin off: every task, only the controls, or none")
    ap.add_argument("--no-drift", action="store_true")
    ap.add_argument("--port-base", type=int, default=int(os.environ.get("EVAL_PORT_BASE", "19030")),
                    help="sink, SMTP sink, scripted endpoint and proxy trap use this port and the next three")
    ap.add_argument("--timeout", type=int, default=None, help="seconds per Hermes command")
    ap.add_argument("--settle", type=float, default=1.5, help="seconds to wait after a rehearsal before measuring")
    ap.add_argument("--max-cost", type=float, default=float(os.environ.get("EVAL_MAX_COST", "0.50")),
                    help="stop starting tasks once the runs' own cost_usd sum reaches this (real driver)")
    ap.add_argument("--work", default=None, help="folder for workspaces and logs (default: next to the home)")
    ap.add_argument("--out", default=None, help="results folder (default eval/results)")
    a = ap.parse_args(argv)
    if a.timeout is None:
        a.timeout = 240 if a.driver == "scripted" else 600
    if a.tasks == "real":
        sel = T.REAL_DEFAULT
    else:
        sel = [x for x in a.tasks.split(",") if x] or [t["id"] for t in T.TASKS]
    bad = [x for x in sel if x not in T.BY_ID]
    if bad:
        sys.exit(f"unknown task(s): {', '.join(bad)}; known: {', '.join(T.BY_ID)}")
    run = Run(a)
    doc = {"schema": SCHEMA, "run_id": run.ts, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ended": None,
           "driver": a.driver, "model": run.model, "provider": run.provider, "git_commit": git_head(), "git_dirty": git_dirty(),
           "plugin": {"name": names.PLUGIN_NAME, "version": names.VERSION}, "repetitions": a.reps,
           "tasks_selected": sel, "off_mode": a.off, "ports": {"sink": run.sink_port, "smtp": run.smtp_port,
                                                              "stub": run.stub_port, "trap": run.trap_port},
           "work_dir": run.work, "controls": None, "results": [], "totals": None, "stopped_early": None}
    out = os.path.join(run.out_dir, "results.json")

    def save():
        doc["totals"] = totals(doc["results"])
        with open(out + ".tmp", "w") as f:
            json.dump(doc, f, indent=1, default=str)
        os.replace(out + ".tmp", out)

    print(f"eval {run.ts}: driver {a.driver} model {run.model} provider {run.provider}; {len(sel)} task(s) x {a.reps}",
          file=sys.stderr)
    try:
        run.start()
        doc["controls"] = run.controls()
        save()
        if not all(doc["controls"].values()):
            doc["stopped_early"] = "a positive control failed: the instruments cannot be trusted"
            print(doc["stopped_early"], doc["controls"], file=sys.stderr)
            return 2
        for rep in range(1, a.reps + 1):
            for tid in sel:
                if not run.scripted and run.cost >= a.max_cost:
                    doc["stopped_early"] = f"cost cap reached: {run.cost:.4f} >= {a.max_cost}"
                    break
                t0 = time.monotonic()
                r = run.task(T.BY_ID[tid], rep)
                doc["results"].append(r)
                save()
                print(f"  {tid} r{rep}: {'PASS' if r['verdict']['pass'] else 'FAIL'} in {time.monotonic() - t0:.1f}s"
                      + ("" if r["verdict"]["pass"] else "  " + "; ".join(r["verdict"]["failures"])[:300]),
                      file=sys.stderr)
            if doc["stopped_early"]:
                break
    finally:
        run.stop()
        doc["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        doc["cost_usd_reported"] = round(run.cost, 6)
        save()
    print(table(doc["results"]))
    print(json.dumps(doc["totals"]))
    print(f"results: {out}")
    return 0 if doc["results"] and all(r["verdict"]["pass"] for r in doc["results"]) and not doc["stopped_early"] else 1


if __name__ == "__main__":
    sys.exit(main())
