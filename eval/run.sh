#!/bin/bash
# Run the evaluation harness.
#
#   EVAL_HOME=<scratch Hermes home> bash eval/run.sh [options]
#
# Options (passed to eval/run.py):
#   --driver scripted|openrouter   scripted (default): no model, fixed tool calls through Hermes' real loop
#   --model M --provider P         defaults: scripted/evalstub, or deepseek/deepseek-v4-flash/openrouter
#   --tasks t01_notes,t04_...      a subset (default all; "real" = the real-model subset)
#   --reps N                       repetitions (default 1)
#   --off all|control|none         also run each task with the plugin off (default all)
#   --port-base 19030              sink, SMTP sink, scripted endpoint, proxy trap = base .. base+3
#   --max-cost 0.50                real driver: stop starting tasks once the runs' own cost reaches this
#   --no-drift  --timeout S  --settle S  --work DIR  --out DIR
# Environment:
#   EVAL_PYTHON   python >= 3.10 for the runner (default python3)
#   EVAL_METER    optional command run before and after a real-model run (exit 3 = cap reached: no run)
#   EVAL_NO_SETUP=1  skip installing the plugin and the kit into the home
set -uo pipefail
: "${EVAL_HOME:?set EVAL_HOME to a scratch Hermes home (never ~/.hermes)}"
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
EVAL_HOME="$(cd "$EVAL_HOME" && pwd -P)" || exit 2
case "$EVAL_HOME" in "$(cd ~/.hermes 2>/dev/null && pwd -P)") echo "refusing: that is ~/.hermes"; exit 2;; esac
PY="${EVAL_PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
BASE=19030
DRIVER=scripted
prev=""
for x in "$@"; do
  [ "$prev" = "--port-base" ] && BASE="$x"
  [ "$prev" = "--driver" ] && DRIVER="$x"
  prev="$x"
done
if [ "${EVAL_NO_SETUP:-0}" != "1" ]; then
  bash "$REPO/eval/setup_home.sh" "$EVAL_HOME" "$((BASE + 2))" || exit 2
fi
meter() {
  [ -n "${EVAL_METER:-}" ] || return 0
  bash -c "$EVAL_METER \"eval $1\""; local rc=$?
  [ $rc -eq 3 ] && { echo "meter: cap reached; no model calls"; exit 3; }
  return 0
}
[ "$DRIVER" = "scripted" ] || meter "before"
# in a subshell: the after-meter below must run from the caller's folder (EVAL_METER may be a relative path)
(cd "$REPO/eval" && "$PY" -B run.py --home "$EVAL_HOME" "$@"); rc=$?
[ "$DRIVER" = "scripted" ] || meter "after"
exit $rc
