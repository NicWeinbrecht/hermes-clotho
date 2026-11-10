# Clotho: measured results

Second measurement, 2026-11-10. Every number below comes from a results file in `eval/results/`; the folder is
named next to each table.

## What this measures

Clotho rehearses a Hermes task with every side effect captured into a ledger, then applies the ticked effects
with no model calls. For 11 realistic side-effecting tasks this document reports: how many of each task's
effects the ledger holds, whether anything escaped while the task ran, whether Apply lands exactly the ticked
effects, whether a file changed by hand stops Apply, how many model calls Apply makes, how much slower a
rehearsal is than the same task with the plugin off, and what it costs. It also re-checks four failures seen in
the first measurement (same day).

Code measured: the `git_commit` / `git_dirty` fields of each results.json name the revision (scripted b117488;
flash a9e5c10; pro a9e5c10 with demo.py uncommitted; Claude 669817c). Those ids belong to the development
history, which is not published; the repository starts from one release commit. Between b117488 and 669817c the
only plugin changes outside tests are the demo command (demo.py, and three lines in cli.py that register it),
so capture, report and Apply code is the same in every run. Hermes Agent v0.23.0, macOS (APFS clone +
sandbox-exec shell preview). Harness: `eval/` (`eval/README.md`), plus the two harness fixes in section 6.

Changed since the measurement (the eval was not re-run after these; the test suite was): Apply creates an empty
folder that a rehearsal created (finding N2); a rehearsal offers the same tool catalog as a plain run (finding
N1); calls Hermes splits out of a batched `tool_call` keep their bridge label; the plugin was renamed to Clotho
and its terminal colours changed; the cron roll-up names its reports folder relative to the Hermes home. See
section 5 for the two findings and the tests that cover them.

## 1. Method

For each task and repetition (`eval/run.py`):

1. A fresh workspace and a sentinel folder outside it; tree hashes (path, kind, mode, size, mtime, sha256) of
   both and of the Hermes home's `memories/`, `skills/`, `cron/`.
2. `hermes clotho run "<prompt>"` with the plugin on; then new requests at a local HTTP sink, new messages at a
   local SMTP sink, the tree hashes again (escapes), and the ledger scored against a hand-written list of the
   effects a correct run has (`eval/tasks.py`, `expect()`).
3. Drift case (t01: a file the ledger edits is edited by hand; t04: a file the ledger creates is created by
   hand): Apply must exit 4, name the path, change nothing and send nothing.
4. Apply inside a sandbox profile that denies every non-localhost connection, with every proxy variable pointed
   at a local trap: the sink and SMTP sink must receive exactly the effects whose rows were applied, the
   workspace must equal its pre-run content plus exactly the applied file rows, plus per-task checks (cron job
   created, skill written, wiki log line); model calls counted four ways (Apply receipt, scripted-endpoint log,
   proxy trap, sandbox denials).
5. The same task with the plugin off (`hermes -z`, new workspace): sink/SMTP hits and wall time.

All sinks listen on 127.0.0.1. Nothing left the machine except model API calls.

A task run PASSES only if: 0 escapes, 0 missed effects, 0 extra ledger rows, no effect recorded only under
Hermes' bridge name `tool_call`, drift stopped (where defined), Apply exact on sink, SMTP, files and
post-checks, 0 Apply model calls, and the OFF run of a control task hit a sink. Every run is counted; none was
dropped.

Runs:

- scripted driver (fixed tool calls through Hermes' real agent loop, no model), 11 tasks x 3
- `deepseek/deepseek-v4-flash` via OpenRouter, 11 tasks x 3
- `deepseek/deepseek-v4-pro` via OpenRouter, 4 tasks x 1 (t04, t06, t08, t11)
- Claude subscription provider (`claude-subscription-directsdk-experimental` 0.3.4, model haiku), 2 tasks x 1
  (t04 with its OFF control; t06 without an OFF run, to stay inside a call budget)

## 2. Instruments (checked before any number was trusted)

- Positive controls, at the start of every run: the sink logs a POST, the SMTP sink logs a message, the trap
  sees a proxied call, the Apply sandbox blocks and logs a connection to 192.0.2.1 with the curl's pid. All
  true in every results.json below (`controls`).
- OFF controls: t04, t05, t10 must hit a sink with the plugin off. 20/20 did (scripted 9/9, flash 9/9, pro 1/1,
  Claude 1/1). t06, t07, t09 also hit the sink with the plugin off (2, 1, 1 requests every time); the same tasks
  with the plugin on: 0.
- Deliberately wrong ground truth must fail (`eval/negctl.py`, scripted;
  `eval/results/20261110-103653/negctl.json`). 8/8 failed, each for the injected reason:

  ```
  N1 wrong path for an expected file       -> "missed 1 expected effect(s); 1 extra ledger row(s)"
  N2 wrong sink path expected after Apply  -> "sink got [('POST', '/hooks/009d54/deploy')], expected [('POST', '/hooks/009d54/wrong')]"
  N3 wrong SMTP recipient expected         -> "smtp got [('billing@leebakery.test', 'Invoice 1042')], expected [('someone@else.test', 'Invoice 1042')]"
  N4 wrong post-apply file content         -> "post-apply check failed: ('file_equals', 'logs/keep.txt', 'not what is there\n')"
  N5 ground truth missing one effect       -> "1 extra ledger row(s)" (+ sink mismatch)
  N6 drift on a file the ledger never touches -> "drift case did not stop Apply cleanly"
  N7 OFF control on a task that sends nothing -> "OFF control: the instruments saw no effect with the plugin off"
  N8 one POST + one sentinel file injected -> "escape during the rehearsal: ... sink_new 1, sentinel_changed true, total 2"
  ```
- Targeted probes for the four earlier failures (`eval/probes.py`, scripted, plugin on + Apply + plugin off, 3
  repetitions each; `eval/results/20261110-104714/probes.json`). The skill_view probe's instrument is shown to
  see the effect: the three plugin-off runs raised the probe skill's view_count in `skills/.usage.json` to 3; the
  three rehearsals left the file unchanged.

## 3. Results

Sources: `results.json` (per run) and `headline.json` (roll-up, computed from results.json and, for plugin-off
and delegated-child cost, the home's state.db) in each folder:

| driver | folder |
|---|---|
| scripted | `eval/results/20261110-104056/` |
| flash | `eval/results/20261110-105118/` |
| pro | `eval/results/20261110-111747/` |
| Claude | `eval/results/20261110-112333/` (t04), `eval/results/20261110-112440/` (t06) |

```
                                   scripted   flash      pro       Claude    all
task runs passed                   33/33      31/33      3/4       2/2       69/72
expected effects                   351        351        7         4         713
  captured as the named effect     348        347        7         4         706
  recorded only as `tool_call`     0          0          0         0         0
  covered by a can't-preview row   3          3          0         0         6   (t10's urllib POST inside execute_code)
  missed                           0          1          0         0         1
extra ledger rows                  0          2          1         0         3
escapes: HTTP sink requests        0          0          0         0         0
         SMTP sink messages        0          0          0         0         0
         workspace changed         0          0          0         0         0
         sentinel folder changed   0          0          0         0         0
         Hermes state paths        0          0          1         0         1   (see note E)
Apply exit 0, status ok            33/33      33/33      4/4       2/2       72/72
  sink exact after Apply           33/33      33/33      4/4       2/2       72/72
  SMTP exact after Apply           33/33      33/33      4/4       2/2       72/72
  workspace = pre-run + applied    33/33      33/33      4/4       2/2       72/72
  every Apply check incl. post     33/33      32/33      4/4       2/2       71/72  (flash t11 r3, note M)
drift stopped                      6/6        6/6        1/1       1/1       14/14
model calls during Apply (4 ways)  0          0          0         0         0
Apply wall s, median (max)         1.36 (2.87) 1.50 (2.84) 1.55 (2.35) 2.59, 1.57
Apply inside s, median (max)       0.27 (0.62) 0.25 (0.51) 0.32 (0.41) 0.41, 0.24
ON/OFF wall ratio, median          2.66       0.99       1.32      0.81      -
  range                            0.62-3.70  0.12-3.42  1.17-5.50 (1 pair)
  median ON vs OFF s               4.0 / 1.5  12.7 / 13.0 -        13.3 / 16.4
  median ON - OFF s                +2.47      -0.15      -         -3.1
cost per task run, USD             0          0.0061     0.0384    0.0092*
  (rehearsal + plugin-off run + delegated children, Hermes' own estimates from list prices)
*Claude: list-price equivalent reported by Hermes; billed to the Claude subscription, not per token.
```

Per task, flash (11 x 3; captured/expected; OFF hits = sink + SMTP with the plugin off; medians in seconds):

```
task                 pass  captured   extra  escapes  Apply exact  drift  OFF hits   ON    OFF
t01_notes            3/3   6/6        0      0        3/3          3/3    0,0,0      13.2  17.3
t02_cleanup_logs     3/3   120/120    0      0        3/3          -      0,0,0       9.8   5.6
t03_sort_photos      3/3   180/180    0      0        3/3 (10 moves unticked each) -  0,0,0  11.8  13.0
t04_release_webhook  3/3   6/6        0      0        3/3          3/3    1,1,1 ctl  13.5  12.5
t05_invoice_email    3/3   6/6        0      0        3/3          -      1,1,1 ctl  12.2  12.4
t06_standup_chat     3/3   6/6        0      0        3/3 (#bots unticked) - 2,2,2   10.8   9.7
t07_wiki_push        3/3   3/3        0      0        3/3          -      1,1,1       9.2  11.1
t08_cron_digest      2/3   3/3        2      0        3/3          -      0,0,0      30.0   8.8
t09_delegate         3/3   9/9        0      0        3/3          -      1,1,1      26.4  45.3
t10_code_report      3/3   3/6 + 3 covered 0 0       3/3          -      1,1,1 ctl  14.2  15.3
t11_memory_skill     2/3   5/6        0      0        3/3          -      0,0,0      12.0  10.7
```

Pro: t04 PASS, t06 PASS, t11 PASS, t08 FAIL (1 extra row + note E). Claude: t04 PASS (drift stopped, POST body
equal to release_notes.md, OFF control hit), t06 PASS (#team sent, #bots unticked and not sent).

### The three failing runs and what they show

- flash t08 r2: 2 extra rows. The model fetched the Hermes cron docs (web_extract, held as a read that can
  trigger a webhook) and ran `hermes cron add --help 2>&1 || hermes cron --help 2>&1`, captured as "can't
  preview: writes outside the workspace". Both rows are honest captures of calls the model made; they are not in
  the ground truth, so the run fails. The cron job itself was captured and created by Apply.
- pro t08 r1: 1 extra row (`hermes cron --help 2>&1 || true`, same as above) and note E.
- flash t11 r3 (note M): the model called memory and skill_view, never skill_manage, then wrote "Both done —
  memory captured for review and the skill created". The ledger shows 1 effect (the memory write), the report
  says "1 effect", and Apply created no skill (post-check false). The session's own transcript in state.db has
  the same two tool calls. This is the model claiming an effect it did not attempt; the ledger did not follow
  it.

**E.** The one counted Hermes-state change: `cron/dispatch_held.json` in pro t08, written at 11:20:24, inside
that rehearsal (11:19:09-11:20:28). Its content is `{"since": <time>, "last": <time>}`. In Hermes the only
writer is `cron/dispatch_hold.py` `note_held()`, called from the cron ticker in
`cron/scheduler_provider.py:495` when `can_dispatch()` is false; `can_dispatch` is passed only from
`gateway/run_bootstrap.py`. The scratch home ran an idle-exit gateway (gateway-starts.log). Reading: a
gateway's cron ticker stamped a skipped tick while it drained; no job ran and no user content was written. The
harness now lists this file as Hermes housekeeping (section 6, H1). The table counts it as measured.

## 4. The four failures from the first measurement, before and after

Before = the first measurement (`eval/results/20261110-082940`, `eval/results/20261110-084017`). After = this
measurement (same capture and Apply code across its runs).

- **F1 bridged tool calls recorded under the bridge name `tool_call`.**
  Before: 6 effects over 4 flash runs recorded as tool `tool_call`, class unknown, unticked (t06 4, t08 2);
  scripted t08 1/1; default Apply created no cron job (t08 2/2 failed the post-check).
  After: 0 of 713 expected effects recorded as `tool_call` (72 runs). Scripted t08 3/3, flash t08 3/3, pro t08
  1/1: the cronjob_manage row is class state, ticked, and Apply created the job (post-check true). Probes: single
  form to an out-of-scope tool 3/3, batch form with two chat posts in one call 3/3, every row under its real tool
  name; Apply sent exactly those posts.
- **F2 tool_search / tool_describe captured as effects.**
  Before: 13 extra rows over 8 flash runs (t05-t08), class unknown, the model got a simulated result.
  After: 0 extra catalog rows. The model called tool_search in 5 runs and tool_describe in 15 runs (flash
  tool_search 3, tool_describe 12; pro 1, 2; Claude 1, 1); calls.jsonl shows decision "run" for each. Probe q3
  3/3.
- **F3 skill_view writes skills/.usage.json during a rehearsal.**
  Before: t11 2/2 flash runs, hermes_state_changed `["skills/.usage.json"]`.
  After: 0 skills/ changes in 72 runs; skill_view was called in 8 real-model runs (flash 6, pro 2). Probe q4: 3/3
  rehearsals left .usage.json unchanged while the 3 plugin-off runs raised view_count to 3.
- **F4 about 120 s stall on two file writes in one model turn.**
  Before: two write_file in one turn, ON 123.6 / 123.9 / 123.7 s vs OFF 5.6 / 4.7 / 4.5 s.
  After: two write_file in one turn ON 3.85 / 4.54 / 4.20 s (OFF 1.20 / 1.57 / 1.26 s); write_file + patch in
  one turn ON 3.97 / 4.45 / 3.74 s; the same calls in separate turns 3.68 / 4.16 / 3.97 s. Both effects captured
  and applied in every case. Longest real-model rehearsal: 80.3 s (pro t08, 11 model calls; see finding N1);
  longest flash: 64.6 s (t10 r3, whose plugin-off run took 220.5 s).

## 5. Findings in the plugin, and what happened to them

- **N1** In a rehearsal, Hermes' tool catalog did not offer cronjob_manage; with the plugin off it did. Same
  prompt, flash t08 r1:

  ```
  plugin on,  tool_search:   "total_available": 8, ... "matches": []
              tool_describe: {"tools": {}, "errors": {"cronjob_manage": "'cronjob_manage' is not available in
                             this execution context. Don't retry it — state the assumption and proceed with
                             the work."}}
  plugin off, tool_search:   "total_available": 9, ... "matches": ["cronjob_manage"]; tool_describe returns
                             the schema.
  ```

  Effect: the model searched several more times (t08 ON 9-11 model calls vs 3-4 OFF on flash; median ON 30.0 s
  vs OFF 8.8 s; pro 80.3 s vs 14.6 s), read skills and help text, and two of four real-model t08 runs added rows
  that failed the task. The job was still captured correctly (the bridge reaches it). So a rehearsal could show
  a longer path than the real run would take.
  Cause: a plain `hermes -z` runs its agent in the gateway, whose start sets `HERMES_EXEC_ASK=1`, and
  cronjob_manage's availability check reads that flag; `hermes clotho run` runs the agent in its own process,
  where the flag was unset. Status: fixed after the measurement; `hermes clotho run` sets the same flag (it
  does not change approval prompts, which a single-query run clears). Test: `tests/test_capture_bridge.py` `CapCatalogParity.test_same_catalog_as_a_plain_run` (passes in the
  release test run). The t08 timings above were not re-measured.
- **N2** Full test suite on the code of that day (174 tests): 1 failure, outside the measured paths:

  ```
  FAIL test_fork_runner.ForkApplyEmptyFolder.test_applying_a_timeline_that_creates_an_empty_folder
  receipt: "results": [{"id": 2, "class": "file", "summary": "create Data", "result": "failed", "detail":
  "TypeError: join() argument must be str, bytes, or os.PathLike object, not 'NoneType'"}], "status": "failed"
  ```

  Apply of a timeline whose only file effect is an empty created folder failed. The eval tasks have no such
  effect (t03's by-year/ folders are created by mv and applied fine 72/72).
  Status: fixed; Apply now creates the folder. The same test passes in the release test run.
- **N3** Apply's time inside the command rose from a median of 0.015 s (first measurement, flash) to 0.25 s
  (flash) and 0.27 s (scripted); wall time including Hermes start is 1.4-1.6 s median either way. Not a failure;
  noted for the reader. Cause not investigated.

## 6. Harness changes in this round (eval/ only, each with a test)

- **H1** `cron/dispatch_held.json` is listed as Hermes housekeeping, not as an escape. Test
  `eval/test_state_changes.py`; before the change: "FAIL: test_gateway_dispatch_hold_stamp_is_housekeeping ...
  AssertionError: Lists differ: ['cron/dispatch_held.json'] != []"; after: 3/3 OK. jobs.json, output/, skills/,
  memories/ and a near-miss name (dispatch_held.json.tmp) still count. Results above were measured before H1.
- **H2** `eval/setup_home.sh` added a second top-level `providers:` key when the home's config.yaml already had
  one (written by the test suite's own setup), after which every Hermes command in that home failed with "could
  not parse plugin selection: <home>/config.yaml; running with the previous dependencies". Setup now inserts its
  entry into the existing block (`eval/add_provider.py`). Test `eval/test_add_provider.py`; before: "FAIL:
  test_setup_home_uses_it ... setup_home.sh appends a second providers: block"; after 5/5 OK. The test suite's
  setup (`tests/run.sh`) now uses the same helper.
- **H3** `eval/negctl.py` and `eval/probes.py` added so the instrument checks above can be re-run.
- **H4** Local absolute paths in the committed results files were rewritten to
  `/Users/you/projects/hermes-clotho/...`; no number changed.

## 7. Cost and meters

- Hermes' own estimates (list prices): flash 33 rehearsals USD 0.0956 + plugin-off runs and children 0.1061;
  pro 4 rehearsals 0.0790 + 0.0747; total OpenRouter estimate USD 0.355. Per task run (rehearsal + plugin-off):
  flash 0.0061, pro 0.0384.
- OpenRouter key meter: usage 1.480363 before the first model call (10:51:05) and 2.465378 after the last
  (11:25:19), a delta of USD 0.985. The same key served other work in that window, so the delta is an upper
  bound, not this measurement's cost.
- Claude subscription: 14 calls (t04 rehearsal 3 + 1 title, t04 plugin-off 3 + 1 title, t06 rehearsal 5 + 1
  title; meta.json usage and state.db api_call_count). No usage-limit message in any log. That home held only a
  placeholder OpenRouter key, so a misrouted call could not spend.

## 8. Limits

- 3 repetitions per task with flash, 1 with pro, 1 with Claude on 2 tasks. Wall-time ratios with a real model
  move with model latency (flash range 0.12-3.42); the scripted ratio (2.66, +2.5 s) is the fixed cost of the
  clone, sandbox and readback.
- The ground truth is the same list for scripted and real runs; a "missed" or "extra" row can mean the model did
  something else (`on.ledger.attempted_tools` shows what it called). t01's summary text is not scored.
- Hermes' databases (state.db, kanban.db) are not hashed for escapes; memories/, skills/, cron/ are.
- The scripted endpoint is not running in real-model runs, so that one of the four Apply model-call instruments
  is 0 by construction there; the receipt, trap and sandbox counts are live.
- macOS only; the Linux capture-only path was not measured.
- Costs are Hermes' estimates; the shared meter gives only an upper bound.
- t10's second execute_code (a POST from Python) is covered by a can't-preview row, not an http row; it is
  unticked by default, so whether ticking it lands the POST is not measured.

## 9. How to reproduce

Needs: macOS, Hermes Agent v0.23.0, a scratch Hermes home that has run Hermes once (never your real
`~/.hermes`), python >= 3.10.

```
EVAL_HOME=<home> bash eval/run.sh --reps 3 --port-base 19180 --work <abs dir>                  # scripted
EVAL_HOME=<home> EVAL_METER="bash <meter script>" bash eval/run.sh --driver openrouter \
    --model deepseek/deepseek-v4-flash --reps 3 --max-cost 0.30 --port-base 19180 --work <abs dir>
EVAL_HOME=<home> bash eval/run.sh --driver openrouter --model deepseek/deepseek-v4-pro \
    --tasks t04_release_webhook,t06_standup_chat,t08_cron_digest,t11_memory_skill --port-base 19180 --work <abs dir>
# Claude subscription provider: a home with claude-subscription-directsdk-experimental 0.3.4 enabled, no
# OpenRouter key, agent.max_turns 5 in config.yaml as a call cap:
EVAL_HOME=<home> bash eval/run.sh --driver openrouter --provider claude-subscription-directsdk-experimental \
    --model haiku --tasks t04_release_webhook --off control --port-base 19180 --work <abs dir>
python3 eval/negctl.py --home <home> --port-base 19180 --work <abs dir> --out <abs dir>
python3 eval/probes.py --home <home> --port-base 19180 --work <abs dir> --out <abs dir> --reps 3
```

The port base and the next three ports must be free (sink, SMTP sink, scripted endpoint, proxy trap, all on
127.0.0.1). Each run writes `eval/results/<timestamp>/results.json` and keeps every Hermes command's
stdout/stderr under `<work>/<timestamp>/logs/`.
