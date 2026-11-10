#!/bin/bash
# Prepare a scratch Hermes home for the evaluation: install this checkout's plugin and the eval kit
# plugin, and point an "evalstub" provider at the scripted endpoint.
# Usage: bash eval/setup_home.sh <scratch home> <stub port>
# The home must already exist and have run Hermes once. Never point it at a home you care about.
set -euo pipefail
H="$(cd "${1:?usage: setup_home.sh <scratch home> <stub port>}" && pwd -P)"
STUB_PORT="${2:?usage: setup_home.sh <scratch home> <stub port>}"
[ "$H" != "$(cd "$HOME/.hermes" 2>/dev/null && pwd -P)" ] || { echo "refusing: that is ~/.hermes"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
export PYTHONDONTWRITEBYTECODE=1
VPY=$(ls -d "$H"/installs/*/environments/*/venv/bin/python 2>/dev/null | head -1 || true)
SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
if [ -z "${HERMES_CMD:-}" ] && [ -n "$VPY" ] && [ -x "$VPY" ] && [ -f "$SRC/hermes" ]; then
  export HERMES_CMD="$VPY -B $SRC/hermes"
fi
export HERMES_CMD="${HERMES_CMD:-$HOME/.local/bin/hermes}"
bash "$REPO/tests/install_into_home.sh" "$H" >/dev/null
KIT=evalkit
mkdir -p "$H/plugins/$KIT"
cp "$REPO/eval/kit/$KIT/plugin.yaml" "$REPO/eval/kit/$KIT/__init__.py" "$H/plugins/$KIT/"
grep -q -- "- $KIT\$" "$H/config.yaml" || HERMES_HOME="$H" $HERMES_CMD plugins enable "$KIT" >/dev/null
# joins an existing providers: block (a second top-level key makes Hermes refuse the whole file)
PYX="${EVAL_PYTHON:-python3}"
"$PYX" -B "$REPO/eval/add_provider.py" "$H/config.yaml" evalstub "$STUB_PORT" || exit 2
HERMES_HOME="$H" $HERMES_CMD gateway stop >/dev/null 2>&1 || true
NAME=$(sed -n 's/^PLUGIN_NAME = "\([^"]*\)".*/\1/p' "$REPO/names.py")
LIST=$(HERMES_HOME="$H" $HERMES_CMD plugins list 2>/dev/null)
for p in "$NAME" "$KIT"; do
  printf '%s\n' "$LIST" | grep -E -- "^. +$p +. +enabled" >/dev/null || { echo "setup_home: $p is not enabled in $H"; exit 1; }
done
echo "ready: $H ($NAME + $KIT enabled, evalstub -> 127.0.0.1:$STUB_PORT)"
