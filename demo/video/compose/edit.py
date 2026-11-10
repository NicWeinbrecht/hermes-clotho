#!/usr/bin/env python3
"""Builds one cut of the demo video from edl.json in a single encode (no generation loss between steps).

    python3 edit.py cards  LAYOUT BUILD                       render every caption and overlay PNG (render_cards.sh)
    python3 edit.py voice  TAKE.mp3 BUILD                     the voiceover with the edit list's pauses, padded to
                                                              the cut's length, then compose.sh loudnorm (-14 LUFS)
    python3 edit.py video  LAYOUT TAKES OPENER BUILD OUT.mp4  segments + captions + overlays -> BT.709 H.264, no audio
    python3 edit.py report LAYOUT                             footage time ranges and share, from the edit list

LAYOUT is 16x9 or 4x5. TAKES holds the VHS takes as <shot>_<layout>.mp4. OPENER is the graded opener clip
(grade_opener.py). Then: compose.sh mux VIDEO VOICE OUT, compose.sh probe OUT.

Colour: VHS takes are decoded as BT.601 (how VHS writes them), the opener as BT.709 (its tags); everything is
composed in RGB and converted once to BT.709 with explicit tags (setparams), as compose.sh does.
The 16:9 terminal (1600x900) sits unscaled at x 160, y 0 on the 1920x1080 Ink canvas, the 4:5 terminal (1080x1000)
at x 0, y 0 on 1080x1350 (script.md, "Terminal sizes"); captions sit under the terminal.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EDL = json.load(open(os.path.join(HERE, "edl.json")))
FPS = EDL["fps"]
TOTAL = EDL["duration"]
SWS = "accurate_rnd+full_chroma_int"
TAGS = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
CANVAS = {"16x9": (1920, 1080), "4x5": (1080, 1350)}
TERM_AT = {"16x9": (160, 0), "4x5": (0, 0)}
SHOTS = ("s1_hook", "s2_run", "s3_report_apply", "s4_fork", "s5_drift", "s6_cron")


def canvas_q(layout):
    return "1920" if layout == "16x9" else "1350"


def caption_text(text, layout):
    return text if layout == "16x9" else text.replace("\n", " ")


def cards(layout, build):
    out = os.path.join(build, f"cards_{layout}")
    w, h = CANVAS[layout]
    jobs = os.path.join(build, f"jobs_{layout}.tsv")
    with open(jobs, "w") as f:
        for c in EDL["captions"]:
            t = caption_text(c["text"], layout).replace("\n", "%0A")
            f.write(f"cap_{c['id']}\t{w}\t{h}\tkind=caption&canvas={canvas_q(layout)}&text={t}\n")
        for o in EDL["overlays"][layout]:
            q = f"kind={o['kind']}&canvas={canvas_q(layout)}" + (f"&{o['q']}" if o["q"] else "")
            f.write(f"ov_{o['id']}\t{w}\t{h}\t{q}\n")
    return subprocess.call(["bash", os.path.join(HERE, "render_cards.sh"), out, jobs])


def voice(take, build):
    import numpy as np
    os.makedirs(build, exist_ok=True)
    sr = 48000
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", take, "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    x = np.frombuffer(pcm, np.float32).reshape(-1, 2)
    parts, pos = [], 0
    for at, gap in EDL["voice"]["pauses"]:
        cut = int(round(at * sr))
        parts += [x[pos:cut], np.zeros((int(round(gap * sr)), 2), np.float32)]
        pos = cut
    parts.append(x[pos:])
    y = np.concatenate(parts)
    n = int(round(TOTAL * sr))
    if len(y) > n:
        sys.exit(f"the voice with its pauses ({len(y) / sr:.2f} s) is longer than the cut ({TOTAL} s)")
    y = np.concatenate([y, np.zeros((n - len(y), 2), np.float32)])
    raw = os.path.join(build, "voice_edit.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "-",
                    "-c:a", "pcm_s24le", raw], input=y.tobytes(), check=True)
    norm = os.path.join(build, "voice_norm.wav")
    rc = subprocess.call(["bash", os.path.join(HERE, "compose.sh"), "loudnorm", raw, norm], stdin=subprocess.DEVNULL)
    print(norm)
    return rc


def video(layout, takes, opener, build, out):
    W, H = CANVAS[layout]
    tx, ty = TERM_AT[layout]
    inputs, chains, labels = [], [], []
    src_index = {}

    def add_input(args):
        inputs.extend(args)
        return sum(1 for a in inputs if a == "-i") - 1

    t = 0.0
    for i, s in enumerate(EDL["segments"][layout]):
        src = s["src"]
        # frame-exact lengths from the cumulative clock, so no rounding drift builds up against the captions
        nf = round((t + s["dur"]) * FPS) - round(t * FPS)
        t += s["dur"]
        hold = round(s["dur"] - (s["out"] - s["in"]), 4) + 0.2
        if src == "card":
            chains.append(f"color=c=0x121212:s={W}x{H}:r={FPS}:d={s['dur'] + 0.2},format=gbrp,trim=end_frame={nf}[v{i}]")
        else:
            if src not in src_index:
                path = opener if src == "opener" else os.path.join(takes, f"{src}_{layout}.mp4")
                src_index[src] = add_input(["-i", path])
            k = src_index[src]
            trim = f"[{k}:v]trim=start={s['in']}:end={s['out']},setpts=PTS-STARTPTS"
            if src == "opener":
                if layout == "16x9":
                    geo = f"scale={W}:{H}:in_color_matrix=bt709:in_range=tv:flags=lanczos+{SWS},format=gbrp"
                else:   # 4:5: the centre 864x1080 of the 16:9 clip, scaled 1.25x (opener.md, "Framing")
                    geo = f"crop=864:1080:528:0,scale={W}:{H}:in_color_matrix=bt709:in_range=tv:flags=lanczos+{SWS},format=gbrp"
            else:
                geo = (f"scale=in_color_matrix=bt601:in_range=tv:flags={SWS},format=gbrp,"
                       f"pad={W}:{H}:{tx}:{ty}:color=0x121212")
            pad = f",tpad=stop_mode=clone:stop_duration={hold}"
            chains.append(f"{trim},fps={FPS},{geo}{pad},trim=end_frame={nf},setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[base0]")
    cur = "base0"
    cdir = os.path.join(build, f"cards_{layout}")
    over = [(f"cap_{c['id']}", c["from"], c["to"], 0, 0) for c in EDL["captions"]]
    over += [(f"ov_{o['id']}", o["from"], o["to"], o.get("fade_in", 0), o.get("fade_out", 0)) for o in EDL["overlays"][layout]]
    over.sort(key=lambda x: x[1])
    for j, (name, a, b, fi, fo) in enumerate(over):
        k = add_input(["-loop", "1", "-framerate", str(FPS), "-t", f"{b - a:.3f}", "-i", os.path.join(cdir, name + ".png")])
        f = f"[{k}:v]format=rgba"
        if fi:
            f += f",fade=t=in:st=0:d={fi}:alpha=1"
        if fo:
            f += f",fade=t=out:st={b - a - fo:.3f}:d={fo}:alpha=1"
        f += f",setpts=PTS-STARTPTS+{a}/TB[o{j}]"
        chains.append(f)
        chains.append(f"[{cur}][o{j}]overlay=0:0:eof_action=pass:format=gbrp[b{j}]")
        cur = f"b{j}"
    chains.append(f"[{cur}]scale=out_color_matrix=bt709:out_range=tv:flags={SWS},format=yuv420p,{TAGS},"
                  f"trim=duration={TOTAL}[vout]")
    graph = ";\n".join(chains)
    os.makedirs(build, exist_ok=True)
    with open(os.path.join(build, f"graph_{layout}.txt"), "w") as fh:
        fh.write(graph)
    cmd = ["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex_script", os.path.join(build, f"graph_{layout}.txt"),
           "-map", "[vout]", "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
           "-r", str(FPS), "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
           "-color_range", "tv", "-movflags", "+faststart", out]
    return subprocess.call(cmd, stdin=subprocess.DEVNULL)


def report(layout):
    t, real, rows = 0.0, 0.0, []
    for s in EDL["segments"][layout]:
        a, b = t, t + s["dur"]
        if s["src"] in SHOTS:
            real += s["dur"]
            if rows and rows[-1][2] == "real" and abs(rows[-1][1] - a) < 1e-6:
                rows[-1][1] = b
            else:
                rows.append([a, b, "real"])
        t = b
    print(f"{layout}: real plugin footage {real:.2f} s of {t:.2f} s = {100 * real / t:.1f}%")
    for a, b, _ in rows:
        print(f"  {a:6.2f} - {b:6.2f}  ({b - a:.2f} s)")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(2)
    fn = {"cards": lambda: cards(a[1], a[2]), "voice": lambda: voice(a[1], a[2]),
          "video": lambda: video(a[1], a[2], a[3], a[4], a[5]), "report": lambda: report(a[1])}[a[0]]
    sys.exit(fn())
