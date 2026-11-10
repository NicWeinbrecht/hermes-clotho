# Runtime contract

How Clotho plugs into Hermes Agent: what Hermes guarantees, what the plugin relies on, and where the limits
are. Written against Hermes Agent v0.23.0 (2026.9.24). Every `file:line` is in the hermes-agent source tree at
that version; plugin files are named by their path in this repository.

## 1. What the plugin registers

`__init__.py` `register(ctx)`:

| registration | Hermes API | used for |
|---|---|---|
| `tool_execution` middleware | `ctx.register_middleware` (hermes_cli/plugins.py:939-944; kinds in hermes_cli/middleware.py:19-26) | capture: every tool call of an armed rehearsal passes through it (`capture.tool_execution`) |
| `llm_execution` middleware | same | counting model calls, so the Apply receipt can say how many it made (`capture.llm_execution`) |
| `pre_tool_call` hook | `ctx.register_hook` (hermes_cli/plugins.py:935-937) | the fail-closed backstop (section 4) |
| `hermes clotho …` | `ctx.register_cli_command` (hermes_cli/plugins.py:635-646) | run, report, apply, list, check, fork, compare, cron, demo |
| `/clotho on \| off \| report` | `ctx.register_command` (hermes_cli/plugins.py:649-674) | session mode inside a chat |

No capability in `plugin.yaml` is needed for middleware, hooks, CLI or slash commands; the manifest declares
`provides_hooks` and `provides_middleware` so that `hermes plugins validate` can match them against what
`register()` registers. Data lives in `<HERMES_HOME>/plugin-data/clotho/` (plugins/plugin_storage.py:27-33),
never in the plugin folder.

## 2. The `tool_execution` middleware

What Hermes guarantees, from the source and checked by running it:

- **Where it runs.** Every tool call of the agent loop, sequential and concurrent
  (agent/tool_executor.py:753-831), agent-level invokes (agent/agent_runtime_helpers.py:2470-2552), and the
  direct `handle_function_call` path (model_tools.py:836-859), which `execute_code`'s nested calls
  (tools/code_execution_rpc.py:28-30) and connector batches (tools/connectors/dispatch.py:50-54) use. The chain
  runner is hermes_cli/middleware.py:144-150, 162-215.
- **What it receives.** `api_request_id, args, middleware_schema_version ("hermes.middleware.v1"), next_call,
  original_args, session_id, task_id, telemetry_schema_version ("hermes.observer.v1"), tool_call_id, tool_name,
  turn_id`. No working folder, platform or cron identity: those come from `ctx.session_cwd()`
  (hermes_cli/plugins.py:597-600) and the process state Clotho sets when it arms a rehearsal.
- **Short-circuit.** Whatever the callback returns becomes the tool result the model sees. `next_call(args)` is
  single-use (hermes_cli/middleware.py:180-190); not calling it means the real tool never runs. Passing changed
  arguments to `next_call` sends them downstream; this is how file tools are pointed at the rehearsal's work
  clone.
- **Order.** The chain runs in registration order; plugin load order is topological on `requires_plugins`, then
  alphabetical. A middleware registered earlier could run a tool before Clotho sees it, so Clotho checks the
  chain before it arms (section 4).

What happens around a captured (short-circuited) call in the agent loop:

- `pre_tool_call` hooks, guardrails and approval prompts do not run: they sit inside `next_call`
  (agent/tool_executor.py:678-750, `_dispatch_authorized_once`).
- `post_tool_call` fires with status ok (agent/tool_executor.py:1380-1381 concurrent, :1819-1820 sequential), so
  other plugins' post hooks see captured calls as if they had succeeded.
- `transform_tool_result` does not fire for captured registry-tool calls (agent/tool_executor.py:1738).
- The transcript stores the returned result as an ordinary tool message.
- The activity feed shows the arguments as rewritten (the work-clone path), while the transcript keeps the
  model's own; Clotho maps paths back in the results it returns.
- A captured result never carries an `error` key for `write_file` / `patch`: an error result there adds Hermes'
  "File-mutation verifier: … FAILED this turn" footer (agent/turn_explainers.py:116-160).

On the direct `handle_function_call` path the order is `tool_request` middleware, then `pre_tool_call`, then
`tool_execution` middleware (model_tools.py:946-970), and `session_id` may be empty there, so rehearsal state is
looked up by task id as well.

## 3. Processes

- `hermes clotho run "<task>"` runs the agent in its own process through `hermes_cli.oneshot.run_oneshot`
  (hermes_cli/oneshot.py:238-345). The rehearsal is armed in that process only, where the middleware lives;
  if the plugin cannot be imported, the `clotho` command does not exist and nothing runs. Before starting it
  sets `HERMES_EXEC_ASK=1`, the flag a gateway sets at start, so the model is offered the same tools as in a
  plain run (cronjob_manage reads it); approval prompts are unaffected, because a single-query run clears it
  for them.
- Chat sessions run their turns in a gateway (hermes_cli/cli_single_query.py:486-487,
  hermes_cli/gateway_runtime_start.py:43-61). `/clotho on` arms the session in the gateway's memory, keyed by
  session id. The gateway keeps the plugin code it loaded; restart it after updating the plugin.
- Forks start one `hermes clotho run` child per timeline (`fork.py`), all at once. Each child is started
  through a small boot script that, right before the child's `run_oneshot` starts the model, puts the
  per-timeline model-call cap at the front of that child's `llm_execution` chain; the demo's children also
  load the recorded model turns there. If either cannot be installed, the child stops before any model call.
  Over the cap, a call is answered with a "stop now" reply and never reaches the provider (raising inside
  `llm_execution` would fail open into the provider, so the cap never raises).
- `hermes clotho cron <job>` starts one child process per run, each its own rehearsal (`cron.py`).

## 4. Fail closed

Hermes' middleware is fail-open by design: an exception raised by a callback before `next_call` is reported
and the chain continues to the next middleware or the real tool (hermes_cli/middleware.py:201-213, ending in
`return call_at(index + 1, payload)`; the developer docs say "Middleware failures are fail-open",
website/docs/developer-guide/middleware.md:80-82). `pre_tool_call` hook failures never block
(agent/tool_executor.py:661-675). A plugin that fails to import is logged and skipped
(hermes_cli/plugins_loader.py:642); Hermes has no "required plugin" switch.

What Clotho does about each:

1. The middleware body is one try/except around everything the plugin owns. On any error it returns a result
   that says the tool was not run, and never raises before `next_call`. Only allowed reads call `next_call`.
2. The `pre_tool_call` backstop: while a rehearsal is armed, it blocks any effectful call that reaches Hermes'
   real dispatch without the middleware's stamp. In the agent loop that happens only if the middleware fell
   through; on the `handle_function_call` path it fires before the middleware. The backstop itself is a
   catch-all that blocks on any exception.
3. Armed state lives only where the middleware lives: in the `hermes clotho run` process, or in the gateway's
   memory for `/clotho on`. Nothing reads a flag file, so a plugin that failed to load cannot leave a stale
   "armed" flag that nobody honours.
4. Before arming, `hermes clotho run` (and `hermes clotho check`, which prints the same decision) refuses when:
   Clotho's tool middleware is not ahead of every other tool middleware in the process; the model-call counter
   is not registered; `plugins.isolation` is `host` (hermes_cli/plugin_isolation.py; capture across the host
   boundary is not verified); or the provider can run tools outside Hermes' tool path. The provider rule covers
   ACP providers and the Claude subscription provider unless it is version 0.3.4, whose native tools were
   checked off, whether the provider is named with `--provider`, set in `HERMES_INFERENCE_PROVIDER` or
   configured as the default (`cli.selfcheck`, `cli.provider_problem`).

What cannot be closed by a plugin: `/clotho on` keeps its state in the gateway's memory, so a gateway restart or
a failed plugin reload ends the rehearsal and later calls run for real. The Disclosure in `plugin.yaml` says
so.

## 5. Apply

Apply replays ticked effects with no model in the loop:

- File effects are written byte for byte from the rehearsal's blob store.
- Messages, web requests and Hermes-state effects call the real tool implementation by name with the stored
  arguments: `model_tools.discover_builtin_tools()` then `model_tools.handle_function_call(name, args,
  task_id=…)` (signature model_tools.py:886-893). Hooks and middleware are not skipped, so other plugins'
  `pre_tool_call` policies still see what is applied.
- Captured shell commands are re-run for real only when named with `--only`.
- Apply runs in a non-interactive terminal, where Hermes auto-approves its own dangerous-command prompt; the
  tick in the report is the approval. The Disclosure says so.
- Agent-loop tools cannot be replayed outside the loop: `_AGENT_LOOP_TOOLS = {"todo_list", "memory",
  "session_search", "delegate_task"}` (model_tools.py:618-619) and `handle_function_call` answers them with
  "must be handled by the agent loop" (model_tools.py:949-951). Memory writes and similar effects are shown in
  the report but cannot be applied in this version.
- The receipt's model-call count comes from the plugin's own `llm_execution` middleware, which sees every model
  call Hermes makes in that process.

## 6. Cron

- Jobs normally run inside the gateway's scheduler. A plugin cannot add a flag to `hermes cron`: plugin CLI
  discovery is skipped when the first word is a built-in subcommand (hermes_cli/main.py:2735
  `_BUILTIN_SUBCOMMANDS`, :2771-2779, :3239-3246). So cron mode is Clotho's own verb: `hermes clotho cron <job>
  --runs N`.
- `ctx.post_message` is refused outside a live session, cron included (hermes_cli/plugin_messages.py:1-7), so
  cron reports are files: one per run plus a roll-up in `<HERMES_HOME>/plugin-data/clotho/cron-runs/<batch>/`.
- Each run builds the prompt Hermes builds for a scheduled run of the job and runs it as a one-shot rehearsal
  with the toolsets a cron run gets. The job's schedule, history, outputs and notepad are not written, and
  nothing is delivered. Because it is a one-shot, it does not appear in `hermes cron runs`, and Hermes'
  background memory and skill review can run during it (a real cron run skips that review).

## 7. Chat surfaces

- `/clotho report` answers in the chat with the report as plain text. `ctx.post_message` (edit-in-place on
  platforms that support editing; a new message elsewhere; a queued notice in the classic CLI) is not needed for
  that and is not used.
- There is no Desktop pane in this version. The Desktop plugin SDK supports panes
  (website/docs/developer-guide/desktop-plugin-sdk.md:257-296); the terminal report is the primary surface.

## 8. Limits and things to know

1. Middleware is fail-open in Hermes; Clotho's own error handling and the backstop close that for loaded code,
   not for a plugin that fails to import (section 4).
2. Captured calls skip `pre_tool_call` hooks, approvals and `transform_tool_result`, but fire `post_tool_call`
   with status ok.
3. The model's final text often reads as if effects happened. The effect report, not the model's text, is what
   happened (nothing).
4. `hermes -z` sets `HERMES_YOLO_MODE=1` for its process (hermes_cli/oneshot.py:276-277): with the plugin off,
   approvals are bypassed for anything that runs for real.
5. A gateway keeps the plugin code it loaded until it restarts.
6. Read-only tools that reach a third party (`web_search`) run for real and may be metered; page fetches are
   held (see `docs/TOOL_CLASSES.md`).
