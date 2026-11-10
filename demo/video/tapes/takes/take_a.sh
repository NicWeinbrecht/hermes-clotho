#!/bin/bash
# Take A (LIVE MODEL: deepseek/deepseek-v4-flash on OpenRouter, about 4-6 calls).
# Recorded once by `render.sh take-a` inside `asciinema rec --headless`, then played back by the S2 tapes in both
# layouts, so both cuts show the same run. The workspace was seeded by render.sh just before this script starts.
# What is typed on screen is exactly the command that runs next.
source "$CL_TAPES/env.sh" || exit 2
cd "$CL_STAGE/team-notes" || exit 2
TASK="Read notes.md and write a three-line summary of it to summary.md. Tick 'write summary' in todo.md. Delete scratch.tmp. Then POST the summary to http://127.0.0.1:${CL_SINK_PORT}/team with one plain curl command."
CMD="hermes clotho run \"$TASK\" -m deepseek/deepseek-v4-flash --provider openrouter"   # DEPENDS: flags as Hermes 0.23 takes them (check `hermes clotho run --help`)
clear
sleep 0.6
printf '\033[1m$\033[0m '   # the prompt as env.sh draws it (bold, no hue)
# typed look: one character at a time, then the same string is run
for ((i = 0; i < ${#CMD}; i++)); do printf '%s' "${CMD:i:1}"; sleep 0.02; done
sleep 0.4
printf '\n'
eval "$CMD"
rc=$?
sleep 1.5
exit $rc
