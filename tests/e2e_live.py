"""T6: one real end-to-end session with a hosted model, through every hop:
model -> capture -> report -> apply -> sink and file system. It spends real money (about a tenth of a
cent on a cheap model), so it is opt-in and never part of tests/run.sh.

    HERMES_TEST_HOME=<scratch home with OPENROUTER_API_KEY in its .env> \\
    CLOTHO_E2E_MODEL=deepseek/deepseek-v4-flash CLOTHO_E2E_PROVIDER=openrouter \\
    python3 tests/e2e_live.py

Writes everything (workspace, sink log, rehearsal report, apply receipt, verdict.json) under
$CLOTHO_TEST_ROOT/<time>/ (default <dirname of the home>/clotho-tests) and exits non-zero if any check fails.
"""
import json
import os
import sys

import harness as H

MODEL = os.environ.get(H.names.env("E2E_MODEL"), "deepseek/deepseek-v4-flash")
PROVIDER = os.environ.get(H.names.env("E2E_PROVIDER"), "openrouter")


def main() -> int:
    H.require_home()
    srv = H.Server("sink", [H.PY, "-I", os.path.join(H.FIX, "sink.py"), str(H.SINK_PORT), H.path("logs", "sink.jsonl")],
                   H.SINK_PORT)
    H.SINK_LOG = H.path("logs", "sink.jsonl")
    open(H.SINK_LOG, "a").close()
    srv.start()
    checks = {}
    try:
        checks["sink positive control"] = H.sink_selftest()
        ws = H.make_ws("e2e", {
            "notes.md": "# Week 45\n- shipped the invoice export\n- fixed the login timeout\n- two new customers signed\n"
                        "- next: price page and the onboarding email\n",
            "scratch.tmp": "temporary\n"})
        before = H.tree(ws)
        task = (f"In this folder: read notes.md, write a three-line summary of it to summary.md, delete scratch.tmp, "
                f"then post the summary to the team webhook by running exactly this one command: "
                f"curl -s -X POST http://127.0.0.1:{H.SINK_PORT}/e2e/team --data-binary @summary.md")
        run = H.hermes([H.CLI, "run", task, "-m", MODEL, "--provider", PROVIDER], cwd=ws, label="e2e_rehearse",
                       timeout=600)
        rid = None
        import re
        m = re.search(r"(r-\d{8}-\d{6}-[0-9a-f]{4})", run.stderr + run.stdout)
        rid = m.group(1) if m else None
        checks["rehearsal exit 0"] = run.returncode == 0
        checks["rehearsal id found"] = bool(rid)
        if not rid:
            return _done(checks, None)
        checks["sink saw 0 requests during the rehearsal"] = H.sink_requests("/e2e") == []
        checks["workspace unchanged by the rehearsal"] = H.tree(ws) == before
        rep = H.hermes([H.CLI, "report", rid, "--json"], cwd=ws, label="e2e_report_json")
        report = json.loads(rep.stdout)
        with open(H.path("logs", "e2e_report.json"), "w") as f:
            json.dump(report, f, indent=1)
        H.hermes([H.CLI, "report", rid], cwd=ws, label="e2e_report_text")
        rows = report["effects"]
        files = {r["target"]["path"]: r for r in rows if r["class"] == "file"}
        checks["ledger: summary.md written"] = "summary.md" in files
        checks["ledger: scratch.tmp deleted"] = any(r["target"]["path"] == "scratch.tmp" and r["change"] == "deleted"
                                                    for r in rows if r["class"] == "file")
        http = [r for r in rows if r["class"] == "http"]
        checks["ledger: webhook POST captured"] = len(http) == 1 and http[0]["target"]["url"].endswith("/e2e/team")
        meta = report["rehearsal"]
        app = H.apply(rid, ws, "e2e_apply")
        rc = app.receipt or {}
        with open(H.path("logs", "e2e_receipt.json"), "w") as f:
            json.dump(rc, f, indent=1)
        checks["apply ok"] = app.returncode == 0 and rc.get("status") == "ok"
        checks["apply made 0 model calls"] = rc.get("model_calls") == 0
        posts = H.sink_requests("/e2e/team")
        summary = H.read(os.path.join(ws, "summary.md")) if os.path.exists(os.path.join(ws, "summary.md")) else None
        checks["sink got exactly 1 POST after apply"] = len(posts) == 1
        checks["POST body = summary.md as rehearsed"] = bool(posts) and summary is not None and \
            posts[0]["body"] == summary
        checks["summary.md landed"] = summary is not None
        checks["scratch.tmp gone"] = not os.path.exists(os.path.join(ws, "scratch.tmp"))
        return _done(checks, {"rid": rid, "model": MODEL, "provider": PROVIDER, "cost_usd": meta.get("cost_usd"),
                              "model_calls": meta.get("model_calls"), "tool_calls": meta.get("tool_calls"),
                              "rehearsal_seconds": meta.get("seconds"), "apply_seconds": rc.get("seconds"),
                              "receipt": H.read(app.log + ".out.txt").splitlines()[-1] if app.log else None})
    finally:
        srv.stop()


def _done(checks, info) -> int:
    ok = all(checks.values())
    verdict = {"ok": ok, "checks": checks, "info": info, "root": H.root()}
    with open(H.path("logs", "verdict.json"), "w") as f:
        json.dump(verdict, f, indent=1)
    print(json.dumps(verdict, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
