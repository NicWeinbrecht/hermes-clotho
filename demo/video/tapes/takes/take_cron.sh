#!/bin/bash
# Take C (LIVE MODEL: deepseek/deepseek-v4-flash on OpenRouter, 2 runs, about 4 calls each).
# render.sh take-cron makes the paused job "daily-digest" in the scratch home first (no model call), then records
# this script with asciinema; the S6 tapes play it back in both layouts.
source "$CL_TAPES/env.sh" || exit 2
cd "$CL_STAGE/digest" || exit 2
CMD="hermes clotho cron daily-digest --runs 2"
clear
sleep 0.6
printf '\033[1m$\033[0m '   # the prompt as env.sh draws it (bold, no hue)
for ((i = 0; i < ${#CMD}; i++)); do printf '%s' "${CMD:i:1}"; sleep 0.03; done
sleep 0.4
printf '\n'
eval "$CMD"
rc=$?
sleep 1.5
exit $rc
