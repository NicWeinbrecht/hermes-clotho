#!/bin/bash
# Renders caption, title, end-card, README-header and wordmark PNGs with headless Chrome from cards/card.html.
#
#   bash render_cards.sh OUT_DIR JOBS_FILE
#
# JOBS_FILE: one card per line, tab-separated: name, width, height, query string for card.html. Lines starting
# with # are skipped. Example (see cards/jobs.example.tsv):
#   caption_1920	1920	1080	kind=caption&canvas=1920&text=Untick the email. **Keep** the rest.
#
# Environment: FONT_DIR (default $HOME/Library/Fonts) holds Outfit[wght].ttf, SpaceGrotesk[wght].ttf and
# DankMono-Regular.otf; CHROME (default: the standard macOS Google Chrome install).
# Output: OUT_DIR/<name>.png (RGBA, transparent where the card is), OUT_DIR/fonts.log (per card: font load status,
# the caption's text box and the stat numbers' fitted sizes, as the page reports them).
# Exit 1 if any card reports a font that failed to load.
set -u
OUT=${1:?usage: render_cards.sh OUT_DIR JOBS_FILE}; JOBS=${2:?usage: render_cards.sh OUT_DIR JOBS_FILE}
HERE="$(cd "$(dirname "$0")" && pwd)"
FONT_DIR="${FONT_DIR:-$HOME/Library/Fonts}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
mkdir -p "$OUT/build/fonts" "$OUT/chrome-profile"
OUT="$(cd "$OUT" && pwd)"
cp "$HERE/cards/card.html" "$OUT/build/"
# bracket-free copies: brackets in file names upset URLs and ffmpeg arguments
cp "$FONT_DIR/Outfit[wght].ttf" "$OUT/build/fonts/Outfit-wght.ttf" || exit 1
cp "$FONT_DIR/SpaceGrotesk[wght].ttf" "$OUT/build/fonts/SpaceGrotesk-wght.ttf" || exit 1
cp "$FONT_DIR/DankMono-Regular.otf" "$OUT/build/fonts/DankMono-Regular.otf" || echo "warning: Dank Mono missing; the end-card install line will show a serif" >&2
: > "$OUT/fonts.log"
bad=0
while IFS=$'\t' read -r name w h query; do
  case "$name" in ''|'#'*) continue ;; esac
  url="file://$OUT/build/card.html?$(printf '%s' "$query" | sed 's/ /%20/g')"
  "$CHROME" --headless=new --disable-gpu --no-first-run --no-default-browser-check --user-data-dir="$OUT/chrome-profile" \
    --allow-file-access-from-files --hide-scrollbars --force-device-scale-factor=1 --default-background-color=00000000 \
    --window-size="$w,$h" --virtual-time-budget=3000 --screenshot="$OUT/$name.png" "$url" > /dev/null 2>&1
  st=$("$CHROME" --headless=new --disable-gpu --no-first-run --no-default-browser-check --user-data-dir="$OUT/chrome-profile" \
    --allow-file-access-from-files --virtual-time-budget=3000 --dump-dom "$url" 2>/dev/null | grep -o 'data-[a-z-]*="[^"]*"' | tr '\n' ' ')
  printf '%s\t%s\n' "$name" "$st" >> "$OUT/fonts.log"
  case "$st" in *error*) echo "$name: a font failed to load ($st)" >&2; bad=1 ;; esac
  [ -s "$OUT/$name.png" ] && echo "$name.png" || { echo "$name: no PNG" >&2; bad=1; }
done < "$JOBS"
exit $bad
