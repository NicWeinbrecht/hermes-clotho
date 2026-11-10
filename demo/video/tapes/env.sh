# Sourced, hidden, at the start of every tape and every take script. Nothing in here calls a model.
# render.sh exports:
#   CL_REPO   the plugin checkout
#   CL_HOME   a scratch Hermes home with the plugin installed (never ~/.hermes)
#   CL_STAGE  a short neutral folder for every on-screen path, group "staff" (see script.md, "Paths on screen")
#   CL_TRASH  where replaced folders are moved (nothing is deleted)
#   CL_SINK_PORT  the local sink that stands in for the team webhook
#   CL_PLUGIN the plugin's folder name under plugins/ and plugin-data/ (read from names.py PLUGIN_NAME by render.sh)
: "${CL_REPO:?}" "${CL_HOME:?}" "${CL_STAGE:?}" "${CL_TRASH:?}" "${CL_SINK_PORT:?}" "${CL_PLUGIN:?}"
export FORCE_COLOR=1                       # the plugin's colours survive pipes (report.py honours it)
unset NO_COLOR
# Prompt: bold default text (Paper). No hue: red is only for the kept thread (script.md, "Colour on screen").
export PS1=$'\\[\e[1m\\]$\\[\e[0m\\] '
export LESS='-R -X -Ps'                    # pager: colours on, no screen clearing, a bare prompt
hermes() { HX_HOME="$CL_HOME" bash "$CL_REPO/.run/shared/hx.sh" "$@"; }
export -f hermes

# page FILE PATTERN N [SKIP] [NTH]: show N contiguous lines of a saved real output, starting SKIP lines after the
# NTH line (default 1st) that matches PATTERN (matched with colour codes stripped, printed with them kept).
# No trailing newline and the cursor hidden, so N lines fill N rows without scrolling; the tape then holds the
# frame with `read`, and no prompt is drawn. Nothing is reworded, reordered or left out inside the slice.
page() {
  clear
  printf '\033[?25l'
  # Matching ignores every escape code; printing keeps the colour codes (SGR, ending in m) and drops cursor and
  # screen-clearing codes (the saved demo output starts with the ones `clear` printed into the same file).
  awk -v pat="$2" -v n="$3" -v skip="${4:-0}" -v nth="${5:-1}" '
    { plain = $0; gsub(/\033\[[0-9;?]*[A-Za-z]/, "", plain); line = $0; gsub(/\033\[[0-9;?]*[A-Za-ln-z]/, "", line) }
    !on && plain ~ pat { hits++; if (hits == nth) { on = 1; left = skip } }
    on { if (left > 0) { left--; next } if (c < n) { if (c) printf "\n"; printf "%s", line; c++ } }' "$1"
  read -rs _
  printf '\033[?25h'
}

# The take's workspace and run, saved right after the take and restored before every render,
# so both layouts show the same run and Apply always starts from the same state.
snap_dir() { echo "$CL_STAGE/.snap/$1"; }
restore_take() {   # restore_take NAME WORKSPACE_NAME
  local s; s=$(snap_dir "$1")
  [ -d "$s" ] || { echo "no snapshot $s (run the take first)" >&2; return 2; }
  mkdir -p "$CL_TRASH"
  local t; t="$CL_TRASH/$(date +%H%M%S)-$$"
  mkdir -p "$t"
  [ -e "$CL_STAGE/$2" ] && mv "$CL_STAGE/$2" "$t/"
  local rid; rid=$(cat "$s/rid")
  local rd="$CL_HOME/plugin-data/$CL_PLUGIN/$rid"
  [ -e "$rd" ] && mv "$rd" "$t/"
  cp -cR "$s/ws" "$CL_STAGE/$2"
  cp -cR "$s/rundir" "$rd"
}

# Local sink for the webhook: started by PID file, stopped by PID file.
sink_start() {
  mkdir -p "$CL_STAGE/.sink"
  : > "$CL_STAGE/.sink/requests.jsonl"
  python3 -I "$CL_REPO/tests/fixtures/sink.py" "$CL_SINK_PORT" "$CL_STAGE/.sink/requests.jsonl" \
    "$CL_STAGE/.sink/sink.pid" > /dev/null 2>&1 &
  sleep 0.5
}
sink_stop() { [ -f "$CL_STAGE/.sink/sink.pid" ] && kill "$(cat "$CL_STAGE/.sink/sink.pid")" 2>/dev/null; true; }
# What the sink received, one line per request plus the body (the visible command in shot S3 is `sink`).
sink() {
  python3 -I -c '
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
print(f"sink: {len(rows)} request" + ("" if len(rows) == 1 else "s"))
for r in rows:
    print("  " + str(r.get("method")) + " " + str(r.get("path")))
    for line in (r.get("body") or "").splitlines()[:4]:
        print("  | " + line[:52])
' "$CL_STAGE/.sink/requests.jsonl"
}
export -f page restore_take snap_dir sink_start sink_stop sink
cd "$CL_STAGE" || return 2
