"""Clotho for Hermes Agent: rehearse a task, see every effect it would have, untick what
you don't want, then apply exactly the effects you kept, with no model calls."""
from __future__ import annotations

from . import capture, cli, names, slash


def register(ctx):
    capture._CTX = ctx
    # Order matters: the tool middleware must be the first tool_execution middleware in the chain;
    # `hermes clotho run` refuses to start the model if it is not.
    ctx.register_middleware("tool_execution", capture.tool_execution)
    ctx.register_middleware("llm_execution", capture.llm_execution)
    ctx.register_hook("pre_tool_call", capture.pre_tool_call)
    ctx.register_cli_command(name=names.CLI_NAME, help=f"{names.PRODUCT}: {names.PITCH}",
                             setup_fn=cli.setup, handler_fn=cli.handle,
                             description="rehearse a task, review every effect, apply what you keep")
    ctx.register_command(names.SLASH_NAME, handler=slash.handle,
                         description=f"{names.PRODUCT}: on | off | report", args_hint="on|off|report")
