#!/bin/bash
# The one test command:   HERMES_TEST_HOME=<scratch Hermes home> tests/run.sh [unittest names...]
# HERMES_TEST_HOME must be a scratch home that has run Hermes once (never ~/.hermes). The script installs
# this checkout's plugin and the test-only plugin there, points a "scriptstub" provider at the scripted
# endpoint, and runs every tests/test_*.py (or the unittest names given). Nothing leaves the machine; no
# model is called (T6, the live end-to-end run, is a separate opt-in script: tests/e2e_live.py).
# Optional (prefix from names.py ENV_PREFIX): CLOTHO_TEST_PYTHON (python >= 3.10),
# CLOTHO_TEST_SINK_PORT / _STUB_PORT / _TRAP_PORT (default 18940-18942).
set -uo pipefail
: "${HERMES_TEST_HOME:?set HERMES_TEST_HOME to a scratch Hermes home (never ~/.hermes)}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
case "$(cd "$HERMES_TEST_HOME" && pwd -P)" in "$(cd ~/.hermes 2>/dev/null && pwd -P)") echo "refusing: that is ~/.hermes"; exit 2;; esac
P=$(sed -n 's/^ENV_PREFIX = "\([^"]*\)".*/\1/p' "$REPO/names.py")
[ -n "$P" ] || { echo "cannot read ENV_PREFIX from names.py"; exit 2; }
v="${P}TEST_STUB_PORT"; STUB_PORT="${!v:-18941}"
v="${P}TEST_PYTHON"; PY="${!v:-python3}"
export PYTHONDONTWRITEBYTECODE=1
# The stock launcher runs python -I (PYTHONDONTWRITEBYTECODE is ignored there): start Hermes through the
# home's own venv python with -B instead, so test runs write no bytecode into the Hermes install.
VPY=$(ls -d "$HERMES_TEST_HOME"/installs/*/environments/*/venv/bin/python 2>/dev/null | head -1)
HERMES_SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
if [ -z "${HERMES_CMD:-}" ] && [ -x "$VPY" ] && [ -f "$HERMES_SRC/hermes" ]; then export HERMES_CMD="$VPY -B $HERMES_SRC/hermes"; fi
export HERMES_CMD="${HERMES_CMD:-$HOME/.local/bin/hermes}"
bash "$REPO/tests/install_into_home.sh" "$HERMES_TEST_HOME" || exit 2
KIT=testkit   # test-only plugin, tests/fixtures/testkit (harness.TESTKIT)
mkdir -p "$HERMES_TEST_HOME/plugins/$KIT"
cp "$REPO/tests/fixtures/$KIT/plugin.yaml" "$REPO/tests/fixtures/$KIT/__init__.py" "$HERMES_TEST_HOME/plugins/$KIT/"
grep -q -- "- $KIT\$" "$HERMES_TEST_HOME/config.yaml" || HERMES_HOME="$HERMES_TEST_HOME" $HERMES_CMD plugins enable "$KIT" >/dev/null
# inside an existing top-level providers: block when there is one (a second one makes Hermes refuse the file)
python3 -B "$REPO/eval/add_provider.py" "$HERMES_TEST_HOME/config.yaml" scriptstub "$STUB_PORT" || exit 2
HERMES_HOME="$HERMES_TEST_HOME" $HERMES_CMD gateway stop >/dev/null 2>&1 || true
cd "$REPO/tests" || exit 2
if [ $# -gt 0 ]; then
  "$PY" -m unittest -v "$@"
else
  "$PY" -m unittest discover -v -s . -t . -p 'test_*.py'
fi
