# Tests

One command (macOS, Hermes Agent >= 0.23.0 installed at ~/.local/bin/hermes):

    HERMES_TEST_HOME=/path/to/a/scratch/hermes-home tests/run.sh

`HERMES_TEST_HOME` must be a scratch Hermes home that has its dependency environment set up
(never `~/.hermes`). The script copies this checkout's plugin and the test-only plugin
(`fixtures/testkit`) into it, adds a `scriptstub` provider that points at the scripted endpoint,
and runs every `tests/test_*.py`. Optional: `CLOTHO_TEST_PYTHON` (Python >= 3.10),
`CLOTHO_TEST_SINK_PORT` / `CLOTHO_TEST_STUB_PORT` / `CLOTHO_TEST_TRAP_PORT` (default 18940-18942).

No model is called and nothing leaves the machine: `fixtures/scripted_provider.py` replays fixed
tool calls (no inference) so every test goes through Hermes' real agent loop, tool registry and
middleware chain; `fixtures/sink.py` is the local HTTP sink that stands in for webhooks and email.
Logs, workspaces and the sink log of each run land in `<dirname of HERMES_TEST_HOME>/clotho-tests/<time>/`.

| test | proves |
|---|---|
| T1 `T1OnOff` | rehearsal on: the sink sees 0 requests, the workspace hash is unchanged, the ledger holds the file, web request, code and plugin-tool effects; rehearsal off: the same scripted calls reach the sink and write the file, and no ledger is created |
| T2 `T2Escape` | one call per tool class (read, file, file outside the workspace, shell inside / outside / hard link / background / osascript, code, plugin send, unknown-effect plugin tool, web_extract, memory, skills, subagent): workspace, sentinel folder and Hermes memory/skills/cron hashes unchanged, sink empty, violations read from the kernel's sandbox log, subagent's effects captured |
| T3 `T3FailClosed` | a fault inside the middleware, in classification, or one that escapes the catch-all (backstop) blocks every call: nothing written, nothing sent |
| T4 `T4Apply` | apply is deterministic on identical state and a no-op when repeated; drift stops it before any change and names the file; `--only` / `--except`; dependents warning; 0 model calls under a no-network sandbox and an HTTPS proxy trap, with positive controls |
| T5 `T5Concurrent` | two rehearsals of one workspace at once stay separate; applying both stops the second on drift |
| `TSessionMode` | the slash command on / report / off in a resumed session, then apply from the CLI |
| `test_names` | every name comes from `names.py`; `plugin.yaml`, the sandbox profile and the ledger schema and example agree with it; no tracked file carries an old name |
| unit | the two sandbox detection fixes (hard link to an outside file; late writer from a detached child), parsers |
| `test_apply_unit` | apply on synthetic rehearsals: certain dependencies stop it before any change, guessed ones only warn; `--force`; failures in the middle and kills leave a journal so nothing is sent twice; one apply at a time; a folder on an effect's path that became a symlink after the rehearsal stops the apply (a sentinel folder outside the workspace stays unchanged), and each write checks again; created folders, empty or not, are made with their mode |
| `test_apply_default` | `apply` with no id when the newest rehearsal is a fork timeline or a cron run: exit 3, the ids to name, nothing changed or sent; a plain newest rehearsal applies as before; with no rehearsal, `apply` and `report` say so |
| `test_apply_hermes` | the same dependency cases through a real Hermes run, measured at the sink and on the workspace tree |
| `test_apply_selfcheck` | a provider that can run tools outside Hermes' tool path gets the same answer on `--provider`, `HERMES_INFERENCE_PROVIDER` and the configured default: exit 3, no rehearsal armed, no model call |
| `test_report_unit` | the report: 58 columns, the ANSI contract, plain output, counts, collapsed groups, unknown badges and fields, the card, `--json`; execute_code rows show their code, can't-preview commands show the changes that were thrown away, `VIA SYMLINK` rows name the path the agent used, blocked rows show what they tried |
| `test_capture_*` | the tool_call bridge (rows under the real tool, `source.bridge` also when Hermes unwrapped the call), the same tool catalog as a plain `hermes -z`, parallel writes, refused writes inside and outside the workspace, the home mirror, bookkeeping reads, the clone path kept out of the agent's final text |
| `test_fork_*`, `test_cron_*`, `test_demo_*` | forks (isolation, comparison, apply one, cap, providers for bare model ids in a home with no default), cron mode, the demo end to end with its network checks |
| `test_run_sh` | `tests/run.sh` adds the scripted provider inside an existing `providers:` block and installs the demo's data |
| `test_validate` | `hermes plugins validate` on a clean export of the tracked files passes: security scan safe, no rebinding of Hermes core |

The live end-to-end run with a hosted model (it spends a few tenths of a cent) is separate:
`tests/e2e_live.py` (see the script header).
