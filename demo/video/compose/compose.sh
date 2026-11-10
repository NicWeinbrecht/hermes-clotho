#!/bin/bash
# Assembly steps for both cuts. Each subcommand does one job, so the edit can be scripted step by step.
#
#   compose.sh place   IN.mp4 OUT.mp4 16x9|4x5 [TOP]      terminal footage -> BT.709 master on the layout canvas
#   compose.sh caption IN.mp4 CARD.png OUT.mp4 [FROM TO]  overlay a full-canvas RGBA PNG, optionally only FROM..TO s
#   compose.sh still   CARD.png OUT.mp4 16x9|4x5 SECONDS  a title or end card as a BT.709 clip
#   compose.sh concat  OUT.mp4 IN1.mp4 IN2.mp4 ...        join clips that share size, rate and tags (stream copy)
#   compose.sh loudnorm IN.wav OUT.wav [LUFS] [TP]        two-pass loudnorm to -14 LUFS, true peak -1.5 dBTP,
#                                                         with the gain + limiter fallback (see below)
#   compose.sh mux     VIDEO.mp4 AUDIO.wav OUT.mp4        AAC 192k, faststart, stops at the shorter stream
#   compose.sh probe   FILE                               size, rate, colour tags, duration, loudness
#   compose.sh frame   FILE SECONDS OUT.png [MATRIX]      one frame as RGB; MATRIX bt709 (default, masters) or bt601 (raw VHS)
#
# Rules this follows (BRAND.md, "Toolchain"):
# - VHS 0.12.1 writes untagged yuv420p at 25 fps whose colours match its screenshots when decoded as BT.601.
#   Masters are converted once to BT.709 with explicit tags. In ffmpeg 8 the scale filter's frame properties
#   override -color_primaries/-color_trc, so the chain ends with setparams as well.
# - Homebrew ffmpeg here has no drawtext or libass: all text arrives as PNG overlays rendered by render_cards.sh.
# - Every lavfi source is bounded with -t (an unbounded colour source runs forever).
# - loudnorm two-pass silently falls back to dynamic mode when the linear gain would breach the true-peak
#   target, and also for inputs of about 3 s or less. Pass 2 is therefore read back (normalization_type); if it is
#   not "linear", the script applies the measured gain plus a limiter instead and measures again. The result must
#   land within +-1 LU of the target with the true peak at or under the TP target.
#
# Environment: FFMPEG, FFPROBE (default: the ones on PATH); FPS (default 25, VHS's real rate); CRF (default 16);
# DUR (place only: keep the first DUR seconds).
set -u
FF=${FFMPEG:-ffmpeg}; FP=${FFPROBE:-ffprobe}
FPS=${FPS:-25}; CRF=${CRF:-16}
INK=0x121212
TAGS="setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
SWS="accurate_rnd+full_chroma_int"   # without these, 1080-wide frames lost about 1 code value of Ink per generation
TO709="scale=out_color_matrix=bt709:out_range=tv:flags=$SWS,format=yuv420p,$TAGS"
ENC=(-c:v libx264 -preset slow -crf "$CRF" -pix_fmt yuv420p -r "$FPS" -color_primaries bt709 -color_trc bt709
     -colorspace bt709 -color_range tv -movflags +faststart)

die() { echo "compose.sh: $*" >&2; exit 2; }
dur() { "$FP" -v error -show_entries format=duration -of csv=p=0 "$1"; }
canvas() { case "$1" in 16x9) echo 1920x1080 ;; 4x5) echo 1080x1350 ;; *) die "layout must be 16x9 or 4x5" ;; esac; }

place() {  # VHS footage (untagged, BT.601) -> BT.709 master on the canvas
  local in=$1 out=$2 layout=$3 top=${4:-}
  local size; size=$(canvas "$layout"); local W=${size%x*} H=${size#*x}
  local d; d=${DUR:-$(dur "$in")}
  if [ "$layout" = 16x9 ]; then
    # full frame; footage of another size (e.g. 1600x900) is scaled to 1920x1080
    "$FF" -v error -y -i "$in" -filter_complex \
      "[0:v]scale=$W:$H:in_color_matrix=bt601:in_range=tv:flags=lanczos+$SWS,format=gbrp,$TO709[v]" -map "[v]" -an -t "$d" "${ENC[@]}" "$out" < /dev/null
  else
    # Ink canvas; footage scaled to 1080 wide and placed TOP px from the top (default: centred above the caption zone)
    top=${top:-60}
    "$FF" -v error -y -f lavfi -t "$d" -i "color=c=$INK:s=${W}x${H}:r=$FPS" -i "$in" -filter_complex \
      "[0:v]format=gbrp[bg];[1:v]scale=$W:-2:in_color_matrix=bt601:in_range=tv:flags=lanczos+$SWS,format=gbrp[t];[bg][t]overlay=0:$top:shortest=1:format=gbrp,$TO709[v]" \
      -map "[v]" -an -t "$d" "${ENC[@]}" "$out" < /dev/null
  fi
}

caption() {  # BT.709 master + RGBA PNG -> BT.709 master; FROM/TO limit the overlay to a time window
  local in=$1 card=$2 out=$3 from=${4:-} to=${5:-}
  local en=""; [ -n "$from" ] && en=":enable='between(t,$from,$to)'"
  "$FF" -v error -y -i "$in" -loop 1 -i "$card" -filter_complex \
    "[0:v]scale=in_color_matrix=bt709:in_range=tv:flags=$SWS,format=gbrp[t];[1:v]format=rgba[c];[t][c]overlay=0:0:shortest=1:format=gbrp$en,$TO709[v]" \
    -map "[v]" -map 0:a? -c:a copy "${ENC[@]}" "$out" < /dev/null
}

still() {  # card PNG on Ink -> BT.709 clip of SECONDS
  local card=$1 out=$2 layout=$3 secs=$4
  local size; size=$(canvas "$layout")
  "$FF" -v error -y -f lavfi -t "$secs" -i "color=c=$INK:s=$size:r=$FPS" -loop 1 -t "$secs" -i "$card" -filter_complex \
    "[0:v]format=gbrp[bg];[1:v]format=rgba[c];[bg][c]overlay=0:0:shortest=1:format=gbrp,$TO709[v]" \
    -map "[v]" -an -t "$secs" "${ENC[@]}" "$out" < /dev/null
}

concat() {
  local out=$1; shift
  local list; list=$(mktemp -t concat).txt
  for f in "$@"; do printf "file '%s'\n" "$(cd "$(dirname "$f")" && pwd)/$(basename "$f")" >> "$list"; done
  "$FF" -v error -y -f concat -safe 0 -i "$list" -c copy -movflags +faststart "$out" < /dev/null
}

loud_json() {  # print loudnorm's JSON block for a file (measure only)
  "$FF" -hide_banner -nostats -i "$1" -af "loudnorm=I=$2:TP=$3:LRA=11:print_format=json" -f null - 2>&1 < /dev/null \
    | awk '/^\{/,/^\}/'
}
jget() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

loudnorm() {
  local in=$1 out=$2 I=${3:--14} TP=${4:--1.5}
  local m; m=$(loud_json "$in" "$I" "$TP")
  [ -n "$m" ] || die "loudnorm pass 1 printed no JSON"
  local mi mtp mlra mth off
  mi=$(jget input_i <<< "$m"); mtp=$(jget input_tp <<< "$m"); mlra=$(jget input_lra <<< "$m")
  mth=$(jget input_thresh <<< "$m"); off=$(jget target_offset <<< "$m")
  echo "pass 1: I=$mi LUFS TP=$mtp dBTP LRA=$mlra"
  local p2
  p2=$("$FF" -hide_banner -nostats -y -i "$in" -af \
    "loudnorm=I=$I:TP=$TP:LRA=11:measured_I=$mi:measured_TP=$mtp:measured_LRA=$mlra:measured_thresh=$mth:offset=$off:linear=true:print_format=json" \
    -ar 48000 "$out" 2>&1 < /dev/null | awk '/^\{/,/^\}/')
  local kind; kind=$(jget normalization_type <<< "$p2")
  echo "pass 2: normalization_type=$kind"
  if [ "$kind" != "linear" ]; then
    # fallback: gain + limiter, then measure and correct the gain (the limiter removes loudness from peaks), up to 4 rounds.
    # alimiter limits sample peaks; inter-sample (true) peaks can sit up to about 1 dB higher, so it limits 1 dB lower.
    local lim; lim=$(python3 -c "print(round(10**(($TP - 1)/20), 4))")
    local gain; gain=$(python3 -c "print(round($I - ($mi), 2))")
    local round got
    for round in 1 2 3 4; do
      "$FF" -v error -y -i "$in" -af "volume=${gain}dB,alimiter=limit=$lim:level=false:attack=5:release=50,aresample=48000" "$out" < /dev/null
      got=$(loud_json "$out" "$I" "$TP" | jget input_i)
      echo "fallback round $round: gain ${gain} dB + limiter at $(python3 -c "print($TP - 1)") dBFS -> I=$got LUFS"
      python3 -c "import sys; sys.exit(0 if abs($got - ($I)) <= 0.2 else 1)" && break
      gain=$(python3 -c "print(round($gain + ($I) - ($got), 2))")
    done
  fi
  local f; f=$(loud_json "$out" "$I" "$TP")
  local fi_ ftp; fi_=$(jget input_i <<< "$f"); ftp=$(jget input_tp <<< "$f")
  echo "result: I=$fi_ LUFS TP=$ftp dBTP"
  python3 -c "import sys; sys.exit(0 if abs($fi_ - ($I)) <= 1.0 else 1)" || { echo "loudness off target by more than 1 LU" >&2; return 1; }
  python3 -c "import sys; sys.exit(0 if $ftp <= ($TP) + 0.1 else 1)" || { echo "true peak above $TP dBTP" >&2; return 1; }
}

mux() {
  "$FF" -v error -y -i "$1" -i "$2" -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest -movflags +faststart "$3" < /dev/null
}

probe() {
  "$FP" -v error -show_entries stream=codec_type,width,height,r_frame_rate,pix_fmt,color_space,color_primaries,color_transfer,color_range:format=duration,size -of compact "$1"
  if "$FP" -v error -select_streams a -show_entries stream=index -of csv=p=0 "$1" | grep -q .; then
    loud_json "$1" -14 -1.5 | python3 -c 'import json,sys; d=json.load(sys.stdin); print("loudness I=%s LUFS TP=%s dBTP LRA=%s" % (d["input_i"], d["input_tp"], d["input_lra"]))'
  fi
}

frame() {  # decode one frame with an explicit matrix, TV range, as a player treats an HD file
  "$FF" -v error -y -ss "$2" -i "$1" -frames:v 1 -vf "scale=in_color_matrix=${4:-bt709}:in_range=tv:flags=$SWS,format=rgb24" "$3" < /dev/null
}

cmd=${1:-}; shift || true
case "$cmd" in
  place)    [ $# -ge 3 ] || die "place IN OUT 16x9|4x5 [TOP]"; place "$@" ;;
  caption)  [ $# -ge 3 ] || die "caption IN CARD OUT [FROM TO]"; caption "$@" ;;
  still)    [ $# -eq 4 ] || die "still CARD OUT 16x9|4x5 SECONDS"; still "$@" ;;
  concat)   [ $# -ge 2 ] || die "concat OUT IN..."; concat "$@" ;;
  loudnorm) [ $# -ge 2 ] || die "loudnorm IN OUT [LUFS] [TP]"; loudnorm "$@" ;;
  mux)      [ $# -eq 3 ] || die "mux VIDEO AUDIO OUT"; mux "$@" ;;
  probe)    [ $# -eq 1 ] || die "probe FILE"; probe "$@" ;;
  frame)    [ $# -ge 3 ] || die "frame FILE SECONDS OUT.png [MATRIX]"; frame "$@" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
