#!/bin/bash
# Records the takes and renders every terminal shot in both layouts. See ../script.md ("Recording plan").
#
#   CL_HOME=<scratch Hermes home> CL_SINK_PORT=<free port> bash render.sh setup
#   ... CL_METER='bash <meter script> <job> note' bash render.sh take-a       # LIVE MODEL (deepseek-v4-flash)
#   ... CL_METER='...' bash render.sh take-cron                                # LIVE MODEL (2 cron runs)
#   ... bash render.sh shots [s4 s1 s2 s3 s5 s6]                               # no model call; order matters
#
# Also required: CL_TRASH, the folder replaced files are moved to (nothing is deleted).
# Optional: CL_STAGE (default /tmp/cl), CL_OUT (default $CL_STAGE/out).
# The live takes refuse to start without CL_METER ("no meter, no start"); it is run before and after.
# Every command typed on screen is `hermes clotho ...`: this script refuses to run until names.py says CLI_NAME
# is "clotho", so no take or shot can be made under an older name.
set -u
CL_TAPES="$(cd "$(dirname "$0")" && pwd)"
CL_REPO="$(cd "$CL_TAPES/../../.." && pwd)"
: "${CL_HOME:?set CL_HOME to a scratch Hermes home with the plugin installed (never ~/.hermes)}"
: "${CL_SINK_PORT:?set CL_SINK_PORT to a free localhost port}"
CL_HOME="$(cd "$CL_HOME" && pwd -P)"
CL_STAGE="${CL_STAGE:-/tmp/cl}"
CL_OUT="${CL_OUT:-$CL_STAGE/out}"
CL_TRASH="${CL_TRASH:?set CL_TRASH to the folder replaced files are moved to (nothing is deleted)}"
# names from the code, so a later rename cannot leave this script behind
read -r CL_PLUGIN CL_CLI < <(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import names; print(names.PLUGIN_NAME, names.CLI_NAME)' "$CL_REPO")
[ "${CL_CLI:-}" = clotho ] || { echo "refusing: names.py CLI_NAME is '${CL_CLI:-?}', the tapes type 'hermes clotho'" >&2; exit 2; }
export CL_TAPES CL_REPO CL_HOME CL_STAGE CL_OUT CL_TRASH CL_SINK_PORT CL_PLUGIN
[ "$CL_HOME" != "$(cd "$HOME/.hermes" 2>/dev/null && pwd -P)" ] || { echo "refusing: that is ~/.hermes" >&2; exit 2; }

to_trash() { local t; t="$CL_TRASH/$(date +%Y%m%d-%H%M%S)-$$"; mkdir -p "$t"; for p in "$@"; do [ -e "$p" ] && mv "$p" "$t/"; done; true; }
meter() { [ -n "${CL_METER:-}" ] || { echo "no meter, no start: set CL_METER" >&2; exit 3; }
          eval "$CL_METER \"$1\""; local rc=$?; [ $rc -eq 0 ] || { echo "meter exit $rc: no model calls" >&2; exit 3; }; }
newest_rid() { ls -1d "$CL_HOME/plugin-data/$CL_PLUGIN"/r-* 2>/dev/null | sort | tail -1; }

setup() {
  local free; free=$(df -g "$CL_REPO" | awk 'NR==2 {print $4}')
  [ "${free:-0}" -ge 3 ] || { echo "under 3 GB free: stop" >&2; exit 3; }
  lsof -nP -iTCP:"$CL_SINK_PORT" -sTCP:LISTEN && { echo "port $CL_SINK_PORT is taken" >&2; exit 3; }
  mkdir -p "$CL_STAGE" "$CL_OUT" "$CL_STAGE/casts" "$CL_STAGE/.snap"
  # group "staff", as under a user's home: the demo compares replayed `ls -l` output with the recording,
  # and a folder of group "wheel" (plain /tmp) gives "13 of 20" instead of "20 of 20" (seen 2026-11-10)
  chgrp staff "$CL_STAGE"
  # the demo needs demo/ inside the installed plugin (tests/install_into_home.sh copies top-level files only)
  if [ ! -d "$CL_HOME/plugins/$CL_PLUGIN/demo/recordings" ]; then
    mkdir -p "$CL_HOME/plugins/$CL_PLUGIN/demo"
    cp -cR "$CL_REPO/demo/seed" "$CL_REPO/demo/recordings" "$CL_HOME/plugins/$CL_PLUGIN/demo/"
  fi
  ( source "$CL_TAPES/env.sh" && hermes clotho check ) | tail -3
  echo "stage $CL_STAGE · out $CL_OUT · home $CL_HOME"
}

write_notes() { # DIR
  cat > "$1/notes.md" <<'EOF'
# Week 45
- Shipped the invoice export; customers can download a CSV from the billing page.
- Fixed the login timeout that hit people on slow connections.
- Two new customers signed: a bakery chain and a dental clinic.
- Next: the price page and the onboarding email.
EOF
}

seed_notes() {
  to_trash "$CL_STAGE/team-notes"
  mkdir -p "$CL_STAGE/team-notes"
  write_notes "$CL_STAGE/team-notes"
  printf -- '- [ ] write summary\n- [ ] send update\n' > "$CL_STAGE/team-notes/todo.md"
  printf 'temporary\n' > "$CL_STAGE/team-notes/scratch.tmp"
}

take_a() {
  seed_notes
  meter "take-a before"
  local before; before=$(newest_rid)
  ( cd "$CL_STAGE" && asciinema rec --headless --overwrite --window-size 58x24 -q \
      -c "bash $CL_TAPES/takes/take_a.sh" "$CL_STAGE/casts/take_a.cast" )
  meter "take-a after"
  local rd; rd=$(newest_rid)
  [ -n "$rd" ] && [ "$rd" != "$before" ] || { echo "no new run: retake" >&2; exit 3; }
  # accept the take only if the delete and the POST are separate rows (see script.md, "Take A acceptance")
  local except
  except=$(python3 -I - "$rd/ledger.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
dele = [r["id"] for r in rows if r["class"] == "file" and r.get("change") == "deleted"
        and (r.get("target") or {}).get("path") == "scratch.tmp"]
post = [r for r in rows if r["class"] == "http" and "/team" in str((r.get("target") or {}).get("url"))]
if len(dele) != 1 or len(post) != 1:
    sys.exit(f"take rejected: delete rows {dele}, POST rows {len(post)}")
print(dele[0])
EOF
) || { echo "retake (at most 3 takes)" >&2; exit 3; }
  local s="$CL_STAGE/.snap/a"; to_trash "$s"; mkdir -p "$s"
  cp -cR "$CL_STAGE/team-notes" "$s/ws"; cp -cR "$rd" "$s/rundir"
  basename "$rd" > "$s/rid"; echo "$except" > "$s/except"
  ( source "$CL_TAPES/env.sh" && cd team-notes && hermes clotho apply --except "$except" --dry-run )
  echo "take A: $(cat "$s/rid"), untick #$except"
}

take_cron() {
  to_trash "$CL_STAGE/digest"; mkdir -p "$CL_STAGE/digest"
  write_notes "$CL_STAGE/digest"
  # DEPENDS: `hermes cron create` flags as printed by `hermes cron create --help` (Hermes 0.23); no model call
  ( source "$CL_TAPES/env.sh" && hermes cron create --name daily-digest --deliver telegram \
      --workdir "$CL_STAGE/digest" --model deepseek/deepseek-v4-flash --provider openrouter --paused \
      "0 9 * * *" "Read notes.md and POST a three-line digest of it to http://127.0.0.1:$CL_SINK_PORT/digest with one plain curl command." )
  meter "take-cron before"
  ( cd "$CL_STAGE" && asciinema rec --headless --overwrite --window-size 58x24 -q \
      -c "bash $CL_TAPES/takes/take_cron.sh" "$CL_STAGE/casts/take_cron.cast" )
  meter "take-cron after"
}

shot() { # shot name, both layouts
  local lay t
  bash "$CL_TAPES/make_tapes.sh" > /dev/null   # tapes carry the current theme (../theme/clotho-theme.json)
  for lay in 16x9 4x5; do
    t="$CL_TAPES/${1}_$lay.tape"
    if [ "$1" = s3_report_apply ]; then
      sed "s/@EXCEPT@/$(cat "$CL_STAGE/.snap/a/except")/" "$t" > "$CL_OUT/${1}_$lay.tape"; t="$CL_OUT/${1}_$lay.tape"
    fi
    [ "$1" = s4_fork ] && to_trash "$CL_STAGE/demo"
    echo "== $1 $lay"
    ( cd "$CL_OUT" && vhs -q "$t" ) > "$CL_OUT/${1}_$lay.vhs.log" 2>&1; echo "exit=$?"
  done
}

case "${1:-}" in
  setup) setup ;;
  take-a) take_a ;;
  take-cron) take_cron ;;
  shots) shift; [ $# -gt 0 ] || set -- s4 s1 s2 s3 s5 s6
         for s in "$@"; do case $s in s1) shot s1_hook ;; s2) shot s2_run ;; s3) shot s3_report_apply ;;
           s4) shot s4_fork ;; s5) shot s5_drift ;; s6) shot s6_cron ;; esac; done ;;
  *) sed -n 2,12p "$0"; exit 2 ;;
esac
