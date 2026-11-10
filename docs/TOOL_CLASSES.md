# Tool classes

How Clotho treats each tool during a rehearsal, and why. The rules live in `classify.py` (which tools run for
real) and `capture.py` (how the rest are captured); this page states them and lists every built-in tool of
Hermes Agent v0.23.0 with its treatment. `file:line` references are in the hermes-agent source tree at that
version.

## The rule: an allowlist, never a blocklist

A tool runs for real during a rehearsal only when it is on Clotho's short reviewed list AND the registered tool
really is Hermes' built-in one from the expected toolset (not another plugin's tool or an override that reuses
the name; `classify._provenance_ok`). Everything else is captured. A tool Clotho does not know is captured,
never run, and is unticked in the report.

## Classes

| class | which tools | during a rehearsal | in the report |
|---|---|---|---|
| read | `read_file`, `search_files`, `skill_view`, `skills_list`, `session_search`, `read_terminal`, `kanban_show`, `kanban_attachments`, `web_search` | run for real; file reads are pointed at the work clone, so they see earlier writes of the same rehearsal | not an effect; what was read feeds Apply's dependency guesses |
| interactive | `clarify`, `todo_list`, `show_tip`, `focus_pane`, `gui_tour`, `annotate_preview`, `close_terminal` | run for real (no effect outside the session; `clarify` must run or the agent stalls) | not shown |
| catalog read | `tool_search`, `tool_describe` | run for real (Hermes answers them inline from the current tool definitions, model_tools.py:739-742), unless another plugin registered a tool under the same name | not shown |
| delegation | `delegate_task` | in `hermes clotho run`: runs, and the children's own tool calls pass through the same middleware and are captured one by one; in session mode: captured as one effect | the children's effects |
| file | `write_file`, `patch` | run against the work clone (or the overlay for paths outside the workspace) | file effects with diffs, ticked |
| shell | `terminal`, `execute_code` | run inside the work clone under macOS `sandbox-exec`: no network, writes only inside the clone | the file changes as effects, ticked; a command that needs the network, writes outside the workspace, reads a secret or starts a background process becomes one "can't preview" effect, unticked; a single plain `curl`/`wget` is shown as a web request, ticked |
| held read | `web_extract`, `vision_analyze`, `video_analyze`, `x_search`, `desktop_preview`, `a2a_discover`, `feishu_doc_read`, `feishu_drive_list_comments`, `feishu_drive_list_comment_replies`, `yb_query_group_info`, `yb_query_group_members`, `yb_search_sticker`, `read_window_below`, `kanban_list` | captured: a GET to an arbitrary URL can trigger a webhook, these calls are metered, and `kanban_list` promotes tasks whose parents are done (a board write) | `HELD READ`, unticked |
| Hermes state | `memory`, `skill_manage`, `cronjob_manage`, `desktop_project`, `set_theme`, `setup_beat`, `process_manage`, the `kanban_*` writes | captured | ticked, not marked irreversible (it can be edited back); `memory` is shown but cannot be applied in this version |
| outbound | every other built-in tool (messages, platform tools, generation, browser and desktop automation, connections, catalog installs), MCP tools (`mcp__…`), hosted connector tools (`connectors__…`), other plugins' tools | captured, never run; the agent gets an honest result | `IRREVERSIBLE`, ticked |
| unknown | a name the registry does not have | captured, never run | `UNKNOWN TOOL`, unticked |

Every captured call gets this result back, so the model carries on:

```
[clotho] captured: <what it would do> - not executed. This is a rehearsal: the effect is recorded for review and
happens only if the user applies it. Any reply is unknown during the rehearsal. Continue the task as if it
succeeded.
```

## Why these rules (what Hermes already says about its tools)

1. The registry entry has no read-only or destructive flag (tools/registry.py:216-233 `ToolEntry`: name,
   toolset, schema, handler, check_fn, requires_env, is_async, description, emoji, max_result_size_chars,
   dynamic_schema_overrides, execution_timeout). So the plugin keeps its own list.
2. Hermes' own "no effect" list (agent/tool_result_classification.py:16-20 `NO_EFFECT_TOOL_NAMES`: read_file,
   search_files, session_search, skill_view, skills_list, web_extract, web_search, vision_analyze,
   browser_snapshot, browser_get_images, browser_console, read_terminal) is a starting point, not the answer:
   `web_extract` and `vision_analyze` are on it but fetch arbitrary URLs, so Clotho holds them. Its comment says
   "Unknown/plugin/MCP tools stay effect-capable by default", which is the same default Clotho uses.
3. `FILE_MUTATING_TOOL_NAMES = {"write_file", "patch"}` (agent/tool_result_classification.py:10).
4. `agent/tool_guardrails.py:20-32` (`IDEMPOTENT_TOOL_NAMES`, `MUTATING_TOOL_NAMES`) still names tools that are
   not registered in v0.23.0, so it is not used as a tool list. `agent/tool_dispatch_helpers.py:53-66`
   (`_DESTRUCTIVE_PATTERNS`) and `tools/terminal_hints.py:109` (`_READONLY_HEADS`) are heuristics, not safety
   boundaries; Clotho does not rely on them. Shell commands are judged by what they did in the sandbox.
5. Tool Search bridge: `tool_search` / `tool_describe` are catalog reads handled inline (model_tools.py:739-742)
   and run. `tool_call` is unwrapped to the real tool before hooks and middleware in the agent loop
   (agent/tool_executor.py:389-398 `_unwrap_tool_search_call`) and in `handle_function_call`
   (model_tools.py:916-936), so the middleware classifies the underlying tool. A batch that names a local tool is
   split by Hermes into single calls (agent/tool_call_batches.expand_local_tool_batches); Clotho labels every
   entry with the bridge it came through. A batch of connector calls only is captured whole.
6. HTTP: Hermes v0.23.0 has no dedicated tool for an HTTP POST. It arrives as `terminal` (curl) or
   `execute_code` (urllib), so outbound web requests are caught by the shell rules: the sandbox has no network,
   a plain curl/wget is parsed into a web request, and anything else that needed the network is a "can't
   preview" effect.
7. Agent-loop tools (`_AGENT_LOOP_TOOLS = {todo_list, memory, session_search, delegate_task}`,
   model_tools.py:618-619) pass through the `tool_execution` middleware in the loop, but `handle_function_call`
   refuses them ("must be handled by the agent loop", model_tools.py:949-951), so Apply cannot replay them
   without a model in this version.
8. `execute_code`'s nested tool calls use `handle_function_call` (tools/code_execution_rpc.py:28-30):
   `pre_tool_call` fires before the `tool_execution` middleware on that path (model_tools.py:946-970) and
   `session_id` is empty, so the rehearsal is looked up by task id too.
9. MCP: Hermes knows a server tool is read-only only when discovery captured `readOnlyHint=True`
   (tools/mcp_tool_handlers.py:52-56 `_tool_is_read_only`). That is the server's own claim, so Clotho captures
   every MCP tool.
10. Mixed tools (read and write in one tool, chosen by an `action` argument: discord, discord_admin,
    process_manage, cronjob_manage, browser_exec, computer_use, the kanban family) are captured whole.

## Every built-in tool (78 registered in Hermes Agent v0.23.0)

Treatment as `classify.py` decides it for the built-in tool; "default" is the tick in the report. Counts: run
for real 16, run in `run` mode 1, file 2, shell 2, held read 14, Hermes state 19, outbound 24.
(`browser_snapshot`, `browser_get_images` and `browser_console` are on Hermes' no-effect list but not registered
in this version.)

| tool | toolset | Clotho | default | handler (file:line) |
|---|---|---|---|---|
| a2a_call | a2a | outbound: captured, IRREVERSIBLE | ticked | plugins/platforms/a2a/tools.py:172 |
| a2a_discover | a2a | held read: captured | unticked | plugins/platforms/a2a/tools.py:146 |
| a2a_history | a2a | outbound: captured, IRREVERSIBLE | ticked | plugins/platforms/a2a/tools.py:223 |
| a2a_list | a2a | outbound: captured, IRREVERSIBLE | ticked | plugins/platforms/a2a/tools.py:199 |
| a2a_orchestrate | a2a | outbound: captured, IRREVERSIBLE | ticked | plugins/platforms/a2a/tools.py:254 |
| annotate_preview | desktop_ui | runs for real |  | tools/annotate_preview_tool.py:93 |
| browser_cdp | browser-cdp | outbound: captured, IRREVERSIBLE | ticked | tools/browser_cdp_tool.py:503 |
| browser_dialog | browser-cdp | outbound: captured, IRREVERSIBLE | ticked | tools/browser_dialog_tool.py:100 |
| browser_exec | browser-use | outbound: captured, IRREVERSIBLE | ticked | tools/browser_use_cli.py:1020 |
| browser_vault | browser | outbound: captured, IRREVERSIBLE | ticked | tools/browser_vault_tool.py:682 |
| clarify | clarify | runs for real |  | tools/clarify_tool.py:221 |
| close_terminal | desktop_ui | runs for real |  | tools/close_terminal_tool.py:50 |
| computer_use | computer_use | outbound: captured, IRREVERSIBLE | ticked | tools/computer_use_tool.py:17 |
| cronjob_manage | cronjob | Hermes state: captured | ticked | tools/cronjob_tools.py:1247 |
| delegate_task | delegation | runs for real in `run` (children captured); captured in session mode |  | tools/delegate_tool.py:924 |
| desktop_preview | desktop_ui | held read: captured | unticked | tools/preview_tool.py:35 |
| desktop_project | project | Hermes state: captured | ticked | tools/project_tools.py:154 |
| discord | discord | outbound: captured, IRREVERSIBLE | ticked | tools/discord_tool.py:631 |
| discord_admin | discord_admin | outbound: captured, IRREVERSIBLE | ticked | tools/discord_tool.py:631 |
| drive_preview | desktop_ui | outbound: captured, IRREVERSIBLE | ticked | tools/drive_preview_tool.py:147 |
| execute_code | code_execution | shell: runs in the sandboxed clone, or captured as can't preview | ticked / can't preview unticked | tools/code_execution_tool.py:952 |
| feishu_doc_read | feishu_doc | held read: captured | unticked | tools/feishu_doc_tool.py:36 |
| feishu_drive_add_comment | feishu_drive | outbound: captured, IRREVERSIBLE | ticked | tools/feishu_drive_tool.py:27 |
| feishu_drive_list_comment_replies | feishu_drive | held read: captured | unticked | tools/feishu_drive_tool.py:27 |
| feishu_drive_list_comments | feishu_drive | held read: captured | unticked | tools/feishu_drive_tool.py:27 |
| feishu_drive_reply_comment | feishu_drive | outbound: captured, IRREVERSIBLE | ticked | tools/feishu_drive_tool.py:27 |
| focus_pane | desktop_ui | runs for real |  | tools/focus_pane_tool.py:45 |
| gui_tour | desktop_ui | runs for real |  | tools/tour_tool.py:160 |
| image_generate | image_gen | outbound: captured, IRREVERSIBLE | ticked | tools/image_generation_tool.py:817 |
| kanban_attach | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:986 |
| kanban_attach_url | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:1042 |
| kanban_attachments | kanban | runs for real |  | tools/kanban_tools.py:1064 |
| kanban_block | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:811 |
| kanban_comment | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:949 |
| kanban_complete | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:725 |
| kanban_create | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:1094 |
| kanban_heartbeat | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:932 |
| kanban_link | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:1274 |
| kanban_list | kanban | held read: captured | unticked | tools/kanban_tools.py:695 |
| kanban_request_changes | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:919 |
| kanban_request_review | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:873 |
| kanban_schedule | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:852 |
| kanban_show | kanban | runs for real |  | tools/kanban_tools.py:662 |
| kanban_unblock | kanban | Hermes state: captured | ticked | tools/kanban_tools.py:1260 |
| manage_catalog | catalog | outbound: captured, IRREVERSIBLE | ticked | tools/connectors/tool.py:175 |
| manage_connections | connections | outbound: captured, IRREVERSIBLE | ticked | tools/connectors/tool.py:162 |
| memory | memory | Hermes state: captured (not appliable: agent loop) | shown only | tools/memory_tool.py:472 |
| patch | file | file: runs against the work clone | ticked | tools/file_tools.py:1358 |
| process_manage | terminal | Hermes state: captured | ticked | tools/process_registry.py:2726 |
| react_to_message | desktop_ui | outbound: captured, IRREVERSIBLE | ticked | tools/react_to_message_tool.py:115 |
| read_file | file | runs for real |  | tools/file_tools.py:1316 |
| read_terminal | desktop_ui | runs for real |  | tools/read_terminal_tool.py:79 |
| read_window_below | desktop_ui | held read: captured | unticked | tools/read_window_tool.py:45 |
| search_files | file | runs for real |  | tools/file_tools.py:1372 |
| session_search | session_search | runs for real |  | tools/session_search_tool.py:802 |
| set_theme | desktop_ui | Hermes state: captured | ticked | tools/set_theme_tool.py:94 |
| setup_beat | onboarding | Hermes state: captured | ticked | tools/setup_beat_tool.py:212 |
| show_tip | desktop_ui | runs for real |  | tools/tip_tool.py:78 |
| skill_manage | skills | Hermes state: captured | ticked | tools/skill_manager_tool.py:942 |
| skill_view | skills | runs for real |  | tools/skills_tool.py:758 |
| skills_list | skills | runs for real |  | tools/skills_tool.py:754 |
| terminal | terminal | shell: runs in the sandboxed clone, or captured as can't preview | ticked / can't preview unticked | tools/terminal_tool.py:1673 |
| text_to_speech | tts | outbound: captured, IRREVERSIBLE | ticked | tools/tts_tool.py:742 |
| todo_list | todo | runs for real |  | tools/todo_tool.py:334 |
| video_analyze | video | held read: captured | unticked | tools/vision_tools.py:1175 |
| video_generate | video_gen | outbound: captured, IRREVERSIBLE | ticked | tools/video_generation_tool.py:157 |
| vision_analyze | vision | held read: captured | unticked | tools/vision_tools.py:1013 |
| web_extract | web | held read: captured | unticked | tools/web_tools.py:567 |
| web_search | web | runs for real |  | tools/web_tools.py:561 |
| write_file | file | file: runs against the work clone | ticked | tools/file_tools.py:1321 |
| x_search | x_search | held read: captured | unticked | tools/x_search_tool.py:329 |
| xai_video_edit | video_gen | outbound: captured, IRREVERSIBLE | ticked | tools/xai_video_tools.py:110 |
| xai_video_extend | video_gen | outbound: captured, IRREVERSIBLE | ticked | tools/xai_video_tools.py:114 |
| yb_query_group_info | hermes-yuanbao | held read: captured | unticked | tools/yuanbao_tools.py:109 |
| yb_query_group_members | hermes-yuanbao | held read: captured | unticked | tools/yuanbao_tools.py:126 |
| yb_search_sticker | hermes-yuanbao | held read: captured | unticked | tools/yuanbao_tools.py:163 |
| yb_send_dm | hermes-yuanbao | outbound: captured, IRREVERSIBLE | ticked | tools/yuanbao_tools.py:213 |
| yb_send_sticker | hermes-yuanbao | outbound: captured, IRREVERSIBLE | ticked | tools/yuanbao_tools.py:181 |
