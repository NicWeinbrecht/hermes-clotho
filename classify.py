"""Which tool calls run for real during a rehearsal, and how the rest are captured.

Allowlist, never blocklist: a tool runs for real only when it is on RUN_FOR_REAL below AND the
registered tool really is Hermes' built-in one (not a plugin tool or an override that reuses the
name). Everything else is captured. Unknown tools are captured, never run.
"""
from __future__ import annotations

import os

from . import names

# name -> the built-in toolset it must come from (provenance check)
RUN_FOR_REAL = {
    # reads (file reads are pointed at the rehearsal's work clone, so they see earlier writes)
    "read_file": "file",
    "search_files": "file",
    "skill_view": "skills",
    "skills_list": "skills",
    "session_search": "session_search",
    "read_terminal": "desktop_ui",
    "kanban_show": "kanban",
    "kanban_attachments": "kanban",
    "web_search": "web",          # a search query; results are what the agent needs to plan
    # interactive, no effect outside the session
    "clarify": "clarify",
    "todo_list": "todo",          # session-local list
    "show_tip": "desktop_ui",
    "focus_pane": "desktop_ui",
    "gui_tour": "desktop_ui",
    "annotate_preview": "desktop_ui",
    "close_terminal": "desktop_ui",
}

# Tool Search catalog reads (docs/TOOL_CLASSES.md rule 5): Hermes answers them inline from the current tool
# definitions (model_tools._dispatch_bridge_tool), never from the registry, so they have no toolset to check;
# they run for real unless another plugin registered a tool under the same name.
CATALOG_READS = {"tool_search", "tool_describe"}

# Run for real in run mode only: the children's own tool calls pass through this process's
# middleware chain and are captured one by one (process-wide arming).
RUN_IN_RUN_MODE = {"delegate_task": "delegation"}

FILE_TOOLS = {"write_file", "patch"}
SHELL_TOOLS = {"terminal", "execute_code"}
READ_PATH_TOOLS = {"read_file", "search_files"}

# Captured, but not shown as irreversible: Hermes' own state that can be edited back.
STATE_TOOLS = {"memory", "skill_manage", "cronjob_manage", "desktop_project", "set_theme", "setup_beat",
               "process_manage"} | {n for n in (
    "kanban_attach", "kanban_attach_url", "kanban_block", "kanban_comment", "kanban_complete", "kanban_create",
    "kanban_heartbeat", "kanban_link", "kanban_request_changes", "kanban_request_review", "kanban_schedule",
    "kanban_unblock")}

# Tools whose apply needs the agent loop (model_tools refuses them outside it).
AGENT_LOOP_TOOLS = {"todo_list", "memory", "session_search", "delegate_task"}

# Captured by default although Hermes lists some of them as "no effect": a GET to an arbitrary
# URL can trigger a webhook, and these are metered third-party or model calls.
CAPTURE_READS = {"web_extract", "vision_analyze", "video_analyze", "x_search", "desktop_preview",
                 "a2a_discover", "feishu_doc_read", "feishu_drive_list_comments",
                 "feishu_drive_list_comment_replies", "yb_query_group_info", "yb_query_group_members",
                 "yb_search_sticker", "read_window_below",
                 # a "read" that writes: kanban_list promotes tasks whose parents are done (recompute_ready)
                 "kanban_list"}

RECIPIENT_KEYS = ("to", "recipient", "recipients", "email", "chat_id", "channel", "channel_id", "user", "user_id",
                  "target", "thread_id", "url", "urls", "endpoint", "agent", "agent_url", "deliver", "phone", "number")
TEXT_KEYS = ("subject", "message", "text", "body", "content", "prompt", "caption", "html", "data")


def _provenance_ok(name: str, expected_toolset: str) -> bool:
    try:
        from hermes_cli.plugins import get_plugin_manager
        if name in getattr(get_plugin_manager(), "_plugin_tool_names", set()):
            return False
    except Exception:
        return False
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        return entry is not None and getattr(entry, "toolset", None) == expected_toolset
    except Exception:
        return False


def _catalog_ok(name: str) -> bool:
    try:
        from hermes_cli.plugins import get_plugin_manager
        if name in getattr(get_plugin_manager(), "_plugin_tool_names", set()):
            return False
        from tools import tool_search as ts
        return name in (getattr(ts, "TOOL_SEARCH_NAME", None), getattr(ts, "TOOL_DESCRIBE_NAME", None))
    except Exception:
        return False


def decide(name: str, args: dict, mode: str = "run") -> str:
    """Return one of: run, file, shell, outbound, state, unknown."""
    if os.environ.get(names.env("TEST_FAULT")) == "classify":  # test seam (fail-closed test)
        raise RuntimeError("injected fault (classify)")
    if name in RUN_FOR_REAL and _provenance_ok(name, RUN_FOR_REAL[name]):
        return "run"
    if name in CATALOG_READS and _catalog_ok(name):
        return "run"
    if mode == "run" and name in RUN_IN_RUN_MODE and _provenance_ok(name, RUN_IN_RUN_MODE[name]):
        return "run"
    if name in FILE_TOOLS and _provenance_ok(name, "file"):
        return "file"
    if name == "terminal" and _provenance_ok(name, "terminal"):
        return "shell"
    if name == "execute_code" and _provenance_ok(name, "code_execution"):
        return "shell"
    if name in STATE_TOOLS:
        return "state"
    if name.startswith("mcp__") or name.startswith("connectors__"):
        return "outbound"
    try:
        from tools.registry import registry
        if registry.get_entry(name) is None:
            return "unknown"
    except Exception:
        return "unknown"
    try:
        from hermes_cli.plugins import get_plugin_manager
        if name in getattr(get_plugin_manager(), "_plugin_tool_names", set()):
            return "outbound"  # another plugin's tool: we cannot know what it does
    except Exception:
        return "unknown"
    return "outbound"


def is_read_only_name(name: str) -> bool:
    return name in RUN_FOR_REAL or name in CATALOG_READS


def recipients_and_text(args: dict) -> tuple[list, str]:
    recips, texts = [], []
    if not isinstance(args, dict):
        return recips, ""
    for k in RECIPIENT_KEYS:
        v = args.get(k)
        if v:
            recips.append(f"{k}={v}" if not isinstance(v, list) else f"{k}=" + ", ".join(map(str, v)))
    for k in TEXT_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            texts.append(v if k != "subject" else "Subject: " + v)
    return recips, "\n".join(texts)
