#!/bin/bash
# Checks for eval/run.sh itself, no Hermes and no model needed:
#   1. EVAL_METER given as a path relative to the repo root is run both before AND after a real-model run.
# Usage: bash eval/test_run_sh.sh     (exit 0 = all pass)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
T="$(mktemp -d "${TMPDIR:-/tmp}/eval-test-run-sh.XXXXXX")"
mkdir -p "$T/home" "$T/rel"
LOG="$T/meter.log"
# a meter script that only records that it ran, reached through a path relative to the repo root
cat > "$T/rel/meter.sh" <<EOF
#!/bin/bash
echo "\$*" >> "$LOG"
EOF
REL="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$T/rel/meter.sh" "$REPO")"
cd "$REPO"
# /usr/bin/true stands in for the runner's python: run.py is not started
EVAL_NO_SETUP=1 EVAL_HOME="$T/home" EVAL_PYTHON=/usr/bin/true EVAL_METER="bash $REL x" \
  bash eval/run.sh --driver openrouter > "$T/out.txt" 2>&1
rc=$?
fail=0
grep -q "eval before" "$LOG" 2>/dev/null || { echo "FAIL: meter not run before"; fail=1; }
grep -q "eval after" "$LOG" 2>/dev/null || { echo "FAIL: meter not run after (run.sh output: $(tr '\n' ' ' < "$T/out.txt"))"; fail=1; }
[ $rc -eq 0 ] || { echo "FAIL: run.sh exit $rc"; fail=1; }
[ $fail -eq 0 ] && echo "PASS: meter before and after (relative EVAL_METER path)"
mkdir -p "$HOME/hermes-hackathon/trash" 2>/dev/null && mv "$T" "$HOME/hermes-hackathon/trash/" 2>/dev/null || true
exit $fail
