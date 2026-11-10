# Evaluation harness

Measures what Clotho promises, task by task, with instruments that can see a failure:

- **captured vs ground truth**: every task has a hand-written list of the effects a correct run has;
  the ledger is scored as captured / covered without a preview / missed / extra;
- **escapes (must be 0)**: during a rehearsal the HTTP sink gets 0 requests, the SMTP sink 0
  messages, and the workspace, a sentinel folder outside it and Hermes' own state
  (`memories/`, `skills/`, `cron/`) keep the same tree hash;
- **Apply**: the sinks get exactly the ticked effects, the workspace ends up as its pre-run state plus
  exactly the applied file rows, and each effect lands where it should (cron job, skill, wiki log);
- **drift**: a file changed by hand between the rehearsal and Apply stops Apply before any change;
- **0 model calls during Apply**, counted four ways (receipt, scripted-endpoint log, proxy trap,
  sandbox denials of any non-localhost connection);
- **overhead**: wall time with the plugin on against the same task with it off.

With the plugin off the same tasks hit the sinks; the tasks marked as OFF controls must, which
proves the instruments can see an escape. Nothing leaves the machine except the model calls of the
`openrouter` driver.

## Run it

```sh
# a scratch Hermes home that has run Hermes once (never ~/.hermes)
EVAL_HOME=~/scratch/hermes-home bash eval/run.sh                     # every task, scripted driver, no key needed
EVAL_HOME=~/scratch/hermes-home bash eval/run.sh --tasks t01_notes,t04_release_webhook --reps 3
EVAL_HOME=~/scratch/hermes-home bash eval/run.sh --driver openrouter --tasks real --max-cost 0.20
```

`run.sh` installs this checkout's plugin and the kit plugin (`eval/kit/evalkit`) into the home,
adds an `evalstub` provider pointing at the scripted endpoint, then runs `eval/run.py`. Options:
`--driver scripted|openrouter`, `--model`, `--provider`, `--tasks a,b|real`, `--reps N`,
`--off all|control|none`, `--port-base N` (sink, SMTP sink, scripted endpoint and proxy trap use
N..N+3, all on 127.0.0.1), `--max-cost USD` (real driver: stop starting tasks once the runs' own
reported cost reaches it), `--no-drift`, `--timeout`, `--settle`, `--work DIR`, `--out DIR`.
`EVAL_METER` may name a command that is run before and after a real-model run; exit code 3 stops it.
A relative path in it is resolved from the folder `run.sh` was started in (`bash eval/test_run_sh.sh`
checks both calls).

**Drivers.** `scripted` replays fixed tool calls per task through Hermes' real agent loop
(`tests/fixtures/scripted_provider.py`, an OpenAI-compatible endpoint with no model behind it), so
the whole harness runs without a key and with exact ground truth. `openrouter` gives a real model
the task's prompt; the ground truth is the same list, so a "missed" row may also mean the model did
not attempt the effect (`on.ledger.attempted_tools` lists what the model called).

## What runs for each task

1. Fresh workspace and sentinel folder; tree hashes of both and of Hermes' state folders.
2. `hermes clotho run "<prompt>"` (plugin on). Then: sink and SMTP deltas, tree hashes, ledger
   scored against the ground truth.
3. Drift case (tasks with `drift`): edit (or create) one file the ledger touches, run Apply, expect
   exit 4, status `drift`, that path named, the workspace hash unchanged and no sink traffic; then
   put the file back.
4. Apply (`default` = the ticks as offered, `unticked` = the ground-truth items marked `untick`
   passed as `--except`), inside `instruments/nonet.sb` with every proxy variable pointed at the
   trap. Checks: sink and SMTP got exactly the effects whose rows were applied, workspace content
   = pre-run content + applied file rows, the task's post-apply checks, 0 model calls.
5. Plugin off (`hermes -z`, same prompt and script, new workspace): sink and SMTP deltas, no new
   rehearsal folder, wall time.

Before any task the runner checks its instruments: the sink logs a POST, the SMTP sink logs a
message, the trap sees a proxied call, and the sandbox profile blocks and logs a connection to
192.0.2.1 (a documentation address). If one fails, nothing runs.

Hermes rewrites a few files under `cron/` on its own in any process (the ticker heartbeat, its
locks, and `dispatch_held.json`, the times an idle-exit gateway skipped ticks while draining). They
are listed as `hermes_housekeeping`, not counted as escapes; every other change under `memories/`,
`skills/` and `cron/` counts (`python3 eval/test_state_changes.py`).

## Checking the instruments themselves

```sh
# deliberately wrong ground truth (and one injected escape) must fail, each for its own reason
python3 eval/negctl.py --home <scratch home> --port-base N --work DIR --out DIR
# targeted probes: tool_call bridge (single and batch form), catalog reads, skill_view, two file
# writes in one model turn; plugin on, Apply, plugin off, --reps times
python3 eval/probes.py --home <scratch home> --port-base N --work DIR --out DIR --reps 3
```

Both run on the scripted driver (no model) and need a home prepared by `eval/setup_home.sh`.
The harness's own tests: `bash eval/test_run_sh.sh`, `python3 eval/test_state_changes.py`,
`python3 eval/test_add_provider.py` (setup joins an existing `providers:` block; a second top-level
`providers:` key makes Hermes refuse the whole config file).

## Tasks

| id | what it does | covers |
|---|---|---|
| t01_notes | summary.md from notes.md, tick a line in todo.md | file create, file edit, drift (edit) |
| t02_cleanup_logs | `rm logs/*.log` over 40 files | shell rm over many files, delete |
| t03_sort_photos | `mkdir -p by-year/2024 && mv IMG_2024_*.jpg by-year/2024/` over 60 files; Apply leaves 10 moves unticked | shell mv over many files, unticked Apply |
| t04_release_webhook | release_notes.md, then `curl --data-binary @release_notes.md` to a webhook | webhook POST, drift (file created by hand), OFF control |
| t05_invoice_email | email an invoice (kit tool, SMTP), append to sent.log | email, shell edit, OFF control |
| t06_standup_chat | post to #team and #bots (kit tool); Apply leaves #bots unticked | chat message via a plugin tool, unticked Apply |
| t07_wiki_push | `mcp__evalnotes__push` writes a page | MCP-style write tool |
| t08_cron_digest | `cronjob_manage` create (paused, local delivery) | cron job creation |
| t09_delegate | plan.md, then `delegate_task`: the child writes research.md and POSTs | delegated child session |
| t10_code_report | `execute_code` writes report.txt; a second `execute_code` POSTs a metric | execute_code, OFF control |
| t11_memory_skill | `memory` add, `skill_manage` create | memory write, skill write |

The kit plugin's tools have real local effects: `evalkit_chat_post` POSTs to the sink at
`/kit/chat/<channel>`, `evalkit_send_email` sends through the SMTP sink, `mcp__evalnotes__push`
appends to `<sentinel>/mcp_push.log` and POSTs to `/kit/mcp/<page>`.

### Ground truth

`tasks.py`, `expect(c)`: one dict per effect. Matching keys (all optional, all must hold):
`class`, `tool`, `path` (workspace-relative), `path_glob`, `change`, `from_path`, `kind`,
`method`, `url_contains`, `args_contain` ({arg: substring}), `child` (true = made by a delegated
child session). Other keys: `optional` (not counted as missed), `untick` (left out by an
`unticked` Apply), `sink` ({method, path, body_from_file}) and `smtp` ({to, subject}): what the
effect sends when applied; `cover_key`: text that lets a command captured without a preview
"cover" this effect. A move recorded as a delete plus a create counts as captured
(`how: "delete+create"`). A call recorded under Hermes' bridge name `tool_call` (single or batch
form) whose underlying tool and arguments match counts as captured too (`how: "bridge-wrapped"`), but
the task fails: the report then names `tool_call`, not the tool, and the row is unticked by default.

## results.json (schema `clotho.eval.v1`)

`eval/results/<timestamp>/results.json`, rewritten after every task. Frozen example:
`eval/results.example.json` (an excerpt of a real scripted run). Readers ignore unknown fields; fields are added only with a schema bump.

| field | meaning |
|---|---|
| schema, run_id, started, ended | `clotho.eval.v1`; run id = folder timestamp |
| driver, model, provider | `scripted` or `openrouter`, and what was passed to Hermes |
| git_commit, git_dirty, plugin {name, version} | the code measured; git_dirty lists plugin files that differed from HEAD (the home gets a copy of the working tree) |
| repetitions, tasks_selected, off_mode, ports, work_dir | how it was run; work_dir holds workspaces and every command's stdout/stderr |
| controls | {sink_logs_a_post, smtp_logs_a_message, trap_sees_a_proxied_call, sandbox_blocks_internet, sandbox_logs_the_denial, scripted_endpoint_up}: all must be true |
| results[] | one object per task and repetition, below |
| totals | sums over results[] (below) |
| stopped_early | null, or why the run stopped (control failed, cost cap) |
| cost_usd_reported | sum of the rehearsals' own `cost_usd` (meta.json); OFF runs are not included |

`results[]`:

| field | meaning |
|---|---|
| task, title, rep, tag, covers, apply_variant | which task; tag = random suffix used in URLs and names of this run |
| on.exit_code, on.timed_out, on.rid, on.wall_s, on.log | the rehearsal command (wall time of the whole `hermes clotho run`) |
| on.model_calls, tool_calls, cost_usd, seconds_inside, status | from the rehearsal's meta.json |
| on.escapes | {sink_new, smtp_new, workspace_changed, sentinel_changed, hermes_state_changed[], sink_paths[], total}; total must be 0 |
| on.ledger | {expected, optional_expected, captured, covered_unpreviewed, missed[], extra[], matches[{expect, row_ids, how}], rows, leaf_rows, classes, attempted_tools, blocked_rows} |
| drift | null or {kind edit/create, path, exit_code, status, drift_paths, workspace_unchanged, sink_new, smtp_new, receipt_model_calls, pass} |
| apply | null or {variant, flags, exit_code, status, selected, applied, applied_ids, failed_ids, not_appliable, wall_s, seconds_inside, log, model_calls {receipt, stub_delta, trap_delta, nonet_denials, denial_samples}, sink {expected, got, exact, body_matches_file?}, smtp {expected, got, exact}, files {match, mismatches, file_rows_applied, file_rows_unticked}, post_checks[{check, ok}], failures[], pass} |
| off | null or {exit_code, wall_s, sink_new, smtp_new, workspace_changed, sink_paths, expected_sink, expected_smtp, new_rehearsal_dirs, escape_visible, control, log} |
| overhead | null or {on_wall_s, off_wall_s, ratio, delta_s} |
| verdict | {pass, failures[]} |

`totals`: task_runs, passed, expected_effects, captured, covered_unpreviewed, missed, extra, escapes,
applies, applies_ok, apply_model_calls, apply_nonet_denials, drift_cases, drift_stopped, off_runs,
off_controls, off_controls_visible, median_overhead_ratio.

Printed table columns: `esc` escapes, `capt/exp` captured/expected, `cov` covered without a
preview, `apply` status (`*` = unticked variant), `sink` exact sink + SMTP match, `files` workspace
matches the ledger, `mcalls` model or internet calls during Apply (all four instruments added),
`drift` stopped, `off` sink+SMTP hits with the plugin off (`!` = control), `x` ON/OFF wall-time ratio.

## Known limits

- Hermes' state folders are checked by tree hash during the rehearsal, but Hermes' databases
  (`state.db`, `kanban.db`) are not.
- The scripted driver measures the plugin, not a model; the openrouter driver measures both.
- OFF runs change the scratch home for real (a cron job, a skill, a memory line): that is the point.
