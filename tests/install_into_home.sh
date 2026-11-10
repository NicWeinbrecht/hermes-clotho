#!/bin/bash
# Copy this checkout's plugin files into a Hermes home and enable the plugin there.
# Usage: tests/install_into_home.sh <HERMES_HOME>   (never point it at a home you care about)
set -euo pipefail
HOME_DIR="$1"
SRC="$(cd "$(dirname "$0")/.." && pwd -P)"
NAME=$(sed -n 's/^name: *//p' "$SRC/plugin.yaml")
DEST="$HOME_DIR/plugins/$NAME"
mkdir -p "$DEST"
for f in "$SRC"/plugin.yaml "$SRC"/*.py "$SRC"/*.sb; do cp "$f" "$DEST/"; done
# the demo's seeded folder and recorded model turns (git-tracked files only; nothing else under demo/).
# The git route only when this checkout is itself the top of a work tree: a plain copy that sits inside
# some other work tree would list nothing there.
mkdir -p "$DEST/demo"
TOP="$(git -C "$SRC" rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$TOP" ] && TOP="$(cd "$TOP" && pwd -P)"
if [ "$TOP" = "$SRC" ]; then
  (cd "$SRC" && git ls-files -z -- demo/seed demo/recordings | rsync -a --from0 --files-from=- ./ "$DEST/")
else
  for d in seed recordings; do [ -d "$SRC/demo/$d" ] && cp -R "$SRC/demo/$d" "$DEST/demo/"; done
fi
export PYTHONDONTWRITEBYTECODE=1
HX="${HERMES_CMD:-$HOME/.local/bin/hermes}"
if ! grep -q -- "- $NAME" "$HOME_DIR/config.yaml" 2>/dev/null; then
  HERMES_HOME="$HOME_DIR" $HX plugins enable "$NAME" >/dev/null
fi
HERMES_HOME="$HOME_DIR" $HX gateway stop >/dev/null 2>&1 || true
echo "installed $NAME into $DEST"
