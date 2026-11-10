#!/bin/bash
# Checks the shell helpers in env.sh that the tapes call on screen, without Hermes and without a model:
#   sink   prints what the local webhook sink received (shot S3 types it)
#   page   prints N lines of a saved output, colour codes kept, no trailing newline
# Usage: bash test_env.sh    (exit 0 = all pass)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d -t clotho-env-test)"
fail=0
check() { # name expected actual
  if [ "$2" == "$3" ]; then echo "ok   $1"; else echo "FAIL $1"; echo "  expected: $(printf '%q' "$2")"; echo "  got:      $(printf '%q' "$3")"; fail=1; fi
}
mkdir -p "$T/stage/.sink"
printf '%s\n' '{"ts": 1, "method": "POST", "path": "/team", "body": "line one\nline two"}' > "$T/stage/.sink/requests.jsonl"
out=$(CL_REPO="$HERE/../../.." CL_HOME="$T/home" CL_STAGE="$T/stage" CL_TRASH="$T/trash" CL_SINK_PORT=1 CL_PLUGIN=x \
  bash -c 'source "$1/env.sh" < /dev/null && sink' _ "$HERE" 2>&1)
check "sink prints one request with its body" $'sink: 1 request\n  POST /team\n  | line one\n  | line two' "$out"

: > "$T/stage/.sink/requests.jsonl"
out=$(CL_REPO="$HERE/../../.." CL_HOME="$T/home" CL_STAGE="$T/stage" CL_TRASH="$T/trash" CL_SINK_PORT=1 CL_PLUGIN=x \
  bash -c 'source "$1/env.sh" < /dev/null && sink' _ "$HERE" 2>&1)
check "sink with no request" "sink: 0 requests" "$out"

printf 'a\n\033[2mskip\033[22m\n\033[31mhead x\033[39m\nb\nc\n' > "$T/out.ansi"
out=$(CL_REPO="$HERE/../../.." CL_HOME="$T/home" CL_STAGE="$T/stage" CL_TRASH="$T/trash" CL_SINK_PORT=1 CL_PLUGIN=x \
  bash -c 'source "$1/env.sh" && page "$2" "^head" 2 1' _ "$HERE" "$T/out.ansi" < /dev/null 2>/dev/null)
check "page prints the slice after the match, codes kept, no trailing newline" $'b\nc' "$(printf '%s' "$out" | sed $'s/\033\\[[0-9;?]*[a-zA-Z]//g; s/\033[[]H//g')"
# the saved demo output starts with the screen-clearing codes `clear` printed into the same file: an anchored
# pattern must still find that first line, and the printed slice must not carry cursor or clear codes
printf '\033[H\033[2J\033[3J\033[1m── Clotho demo ──\033[22m\nsecond\nthird\n' > "$T/demo.ansi"
raw=$(CL_REPO="$HERE/../../.." CL_HOME="$T/home" CL_STAGE="$T/stage" CL_TRASH="$T/trash" CL_SINK_PORT=1 CL_PLUGIN=x \
  bash -c 'source "$1/env.sh" && clear() { :; } && page "$2" "^── [^ ]+ demo ─" 2' _ "$HERE" "$T/demo.ansi" < /dev/null 2>/dev/null)
check "page finds a first line that starts with clear codes, colour codes kept" \
  $'\033[?25l\033[1m── Clotho demo ──\033[22m\nsecond\033[?25h' "$raw"
exit $fail
