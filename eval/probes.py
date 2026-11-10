"""Targeted probes for the four failures traced in the previous measurement. Scripted driver, no model.
Each probe runs through eval/run.py's real Run.task() (plugin on, Apply, plugin off), N repetitions.

    python probes.py --home <scratch home> --port-base N --work DIR --out DIR [--reps 3]

  q1_bridge_single   tool_call bridge, single form, to a tool outside the session's toolsets (cronjob_manage)
  q2_bridge_batch    tool_call bridge, batch form {"calls": [...]}, two chat posts in one call
  q3_catalog_reads   tool_search + tool_describe before a bridged chat post: the lookups must not become rows
  q4_skill_view      skill_view on an existing skill: skills/.usage.json must not change during the rehearsal
  p2_par_write_patch write_file + patch in ONE model turn (previous: ~120 s stall)
  p3_par_two_writes  two write_file in ONE model turn (previous: ~120 s stall)
  p1_seq_write_patch the same calls in separate turns (reference)
Writes <out>/probes.json.
"""
import argparse
import json
import os
import sys

EVAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, EVAL)
os.chdir(EVAL)
import run as R  # noqa: E402
import tasks as T  # noqa: E402

C, B = T.call, T.bridge
FILES = {"notes.md": "a\n", "todo.md": "- [ ] write summary\n"}
SKILL = "probe-skill-w3"


def case(cid, steps, expect, post=None, files=None):
    return {"id": cid, "title": cid, "covers": ["probe"], "files": lambda c: dict(files or FILES),
            "prompt": lambda c: "probe", "script": steps, "expect": expect, "children": lambda c: {},
            "post_apply": post or (lambda c: []), "drift": None, "off_control": False, "apply": "default",
            "real": False}


def chat(ch, text):
    return {"name": "evalkit_chat_post", "arguments": {"channel": ch, "text": text}}


CASES = [
    case("q1_bridge_single",
         lambda c: [[B("cronjob_manage", action="create", name=f"probe-digest-{c.TAG}", schedule="every monday 9am",
                       deliver="local", paused=True, prompt="probe")]],
         lambda c: [{"class": "state", "tool": "cronjob_manage", "args_contain": {"name": c.TAG}}],
         post=lambda c: [("home_contains", "cron", f"probe-digest-{c.TAG}")]),
    case("q2_bridge_batch",
         lambda c: [[{"name": "tool_call", "arguments": {"calls": [chat("#team", f"one {c.TAG}"),
                                                                   chat("#ops", f"two {c.TAG}")]}}]],
         lambda c: [{"class": "outbound", "tool": "evalkit_chat_post", "args_contain": {"channel": "team"},
                     "sink": {"method": "POST", "path": "/kit/chat/team"}},
                    {"class": "outbound", "tool": "evalkit_chat_post", "args_contain": {"channel": "ops"},
                     "sink": {"method": "POST", "path": "/kit/chat/ops"}}]),
    case("q3_catalog_reads",
         lambda c: [[C("tool_search", queries=["chat post"])], [C("tool_describe", names=["evalkit_chat_post"])],
                    [B("evalkit_chat_post", channel="#team", text=f"hello {c.TAG}")]],
         lambda c: [{"class": "outbound", "tool": "evalkit_chat_post", "args_contain": {"channel": "team"},
                     "sink": {"method": "POST", "path": "/kit/chat/team"}}]),
    case("q4_skill_view", lambda c: [[C("skill_view", name=SKILL)]], lambda c: []),
    case("p1_seq_write_patch",
         lambda c: [[C("read_file", path="todo.md")], [C("write_file", path="summary.md", content="s\n")],
                    [C("patch", path="todo.md", old_string="- [ ]", new_string="- [x]")]],
         lambda c: [{"class": "file", "path": "summary.md"}, {"class": "file", "path": "todo.md"}]),
    case("p2_par_write_patch",
         lambda c: [[C("read_file", path="todo.md")],
                    [C("write_file", path="summary.md", content="s\n"),
                     C("patch", path="todo.md", old_string="- [ ]", new_string="- [x]")]],
         lambda c: [{"class": "file", "path": "summary.md"}, {"class": "file", "path": "todo.md"}]),
    case("p3_par_two_writes",
         lambda c: [[C("write_file", path="a.md", content="a\n"), C("write_file", path="b.md", content="b\n")]],
         lambda c: [{"class": "file", "path": "a.md"}, {"class": "file", "path": "b.md"}]),
]


def ensure_skill(home):
    d = os.path.join(home, "skills", SKILL)
    if not os.path.exists(os.path.join(d, "SKILL.md")):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write(f"---\nname: {SKILL}\ndescription: A probe skill that only exists to be viewed\n---\nSay hello.\n")


def main():
    ap = argparse.ArgumentParser()
    for k in ("--home", "--work", "--out"):
        ap.add_argument(k, required=True)
    ap.add_argument("--port-base", type=int, required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    ns = argparse.Namespace(home=a.home, driver="scripted", model=None, provider=None, tasks="", reps=a.reps,
                            off="all", no_drift=True, port_base=a.port_base, timeout=400, settle=1.5, max_cost=0,
                            work=a.work, out=a.out)
    ensure_skill(os.path.realpath(a.home))
    run = R.Run(ns)
    out = {"git_commit": R.git_head(), "git_dirty": R.git_dirty(), "controls": None, "runs": []}
    sel = [t for t in CASES if not a.only or t["id"] in a.only.split(",")]
    try:
        run.start()
        out["controls"] = run.controls()
        for rep in range(1, a.reps + 1):
            for t in sel:
                r = run.task(t, rep)
                lg = r["on"]["ledger"]
                row = {"case": t["id"], "rep": rep, "rid": r["on"]["rid"], "on_exit": r["on"]["exit_code"],
                       "on_wall_s": r["on"]["wall_s"], "on_seconds_inside": r["on"]["seconds_inside"],
                       "off_wall_s": (r["off"] or {}).get("wall_s"), "captured": lg["captured"],
                       "expected": lg["expected"], "extra": lg["extra"],
                       "hows": [m["how"] for m in lg["matches"]], "attempted_tools": lg["attempted_tools"],
                       "escapes": r["on"]["escapes"], "apply_status": (r["apply"] or {}).get("status"),
                       "apply_sink": (r["apply"] or {}).get("sink"), "apply_post": (r["apply"] or {}).get("post_checks"),
                       "off_sink_paths": (r["off"] or {}).get("sink_paths"),
                       "pass": r["verdict"]["pass"], "failures": r["verdict"]["failures"]}
                out["runs"].append(row)
                print(json.dumps({k: row[k] for k in ("case", "rep", "on_wall_s", "off_wall_s", "captured", "expected",
                                                      "hows", "pass")}), flush=True)
                if not row["pass"]:
                    print("   failures:", "; ".join(row["failures"])[:600], flush=True)
    finally:
        run.stop()
        with open(os.path.join(run.out_dir, "probes.json"), "w") as f:
            json.dump(out, f, indent=1, default=str)
        print("written:", os.path.join(run.out_dir, "probes.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
