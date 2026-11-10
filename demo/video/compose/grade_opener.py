#!/usr/bin/env python3
"""Grades the opener clip to the Shears / Vermilion palette before it goes into the cut.

    python3 grade_opener.py IN.mp4 OUT.mp4 [--report]

What it does, per frame (decoded as BT.709, as the clip is tagged):
- Black point to Ink: the clip's background (about #0C0C0C) maps to #121212, so the blacks match the terminal
  footage at the cut; anything darker clips to Ink.
- White point to Paper: the brightest highlights map to #EDE8DF, so no highlight is brighter or cooler than Paper.
- The kept thread to Vermilion: inside the centre column below the spindle, reddish pixels (hue 300-15 degrees,
  saturation above 0.12) are turned to hue 8 degrees with their saturation raised, so the crimson thread the clip
  had (about #CC5B6F, hue 349) reads as Vermilion #E2462F. Weighted by saturation, so the grey threads and the
  wood are left alone.
Output: H.264 yuv444p CRF 12, BT.709 tags, same size and rate. --report prints the thread colour before and after.
"""
import subprocess
import sys

import numpy as np

INK = 18
PAPER = np.array([237, 232, 223], dtype=np.float32)
BLACK_IN = 12.0
TARGET_HUE = 8.0 / 360.0
X0, X1, Y0 = 880, 1060, 112      # the thread column of the 1920x1080 clip, below the spindle


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip().split(",")
    return int(out[0]), int(out[1]), out[2]


def rgb_to_hsv(c):
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    mx = c.max(-1); mn = c.min(-1); d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    rm = m & (mx == r); gm = m & (mx == g) & ~rm; bm = m & ~rm & ~gm
    h[rm] = ((g - b)[rm] / d[rm]) % 6
    h[gm] = (b - r)[gm] / d[gm] + 2
    h[bm] = (r - g)[bm] / d[bm] + 4
    h = h / 6.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0)
    return h, s, mx


def hsv_to_rgb(h, s, v):
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p = v * (1 - s); q = v * (1 - f * s); t = v * (1 - (1 - f) * s)
    out = np.empty(h.shape + (3,), dtype=np.float32)
    for k, (a, b_, c) in enumerate([(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]):
        sel = i == k
        out[sel, 0] = a[sel]; out[sel, 1] = b_[sel]; out[sel, 2] = c[sel]
    return out


def thread_colour(frame):
    col = frame[Y0 + 300:, 945:990].reshape(-1, 3)
    bright = col[col.sum(1) > 300]
    return tuple(int(x) for x in np.median(bright, 0)) if len(bright) else None


def grade(frame):
    f = frame.astype(np.float32)
    # thread to Vermilion, before the levels (hue maths on the original values)
    reg = f[Y0:, X0:X1] / 255.0
    h, s, v = rgb_to_hsv(reg)
    reddish = (h >= 300 / 360) | (h <= 15 / 360)
    w = np.clip((s - 0.12) / 0.15, 0, 1) * reddish * (v > 0.15)
    s2 = np.minimum(s * 1.45, 0.82)
    v2 = np.minimum(v * 1.06, 1.0)
    new = hsv_to_rgb(np.full_like(h, TARGET_HUE), s2, v2)
    reg = reg * (1 - w[..., None]) + new * w[..., None]
    f[Y0:, X0:X1] = reg * 255.0
    # levels: black point to Ink, white point to Paper (per channel)
    f = INK + (f - BLACK_IN) * (PAPER - INK) / (255.0 - BLACK_IN)
    f = np.clip(f, INK, PAPER)
    return np.round(f).astype(np.uint8)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    report = "--report" in sys.argv
    w, h, rate = probe(src)
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", src, "-vf",
                            "scale=in_color_matrix=bt709:in_range=tv:flags=accurate_rnd+full_chroma_int,format=rgb24",
                            "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                            "-r", rate, "-i", "-", "-vf",
                            "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuv444p,"
                            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
                            "-c:v", "libx264", "-preset", "slow", "-crf", "12", "-color_primaries", "bt709",
                            "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv", dst],
                           stdin=subprocess.PIPE)
    n = 0
    size = w * h * 3
    while True:
        buf = dec.stdout.read(size)
        if len(buf) < size:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        out = grade(frame)
        if report and n in (0, 48, 96):
            print(f"frame {n}: thread before {thread_colour(frame)} after {thread_colour(out)}")
        enc.stdin.write(out.tobytes())
        n += 1
    enc.stdin.close()
    enc.wait(); dec.wait()
    print(f"graded {n} frames -> {dst}")
    return 0 if enc.returncode == 0 and n else 1


if __name__ == "__main__":
    sys.exit(main())
