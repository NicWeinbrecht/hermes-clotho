# Assembly scripts

How the two cuts are put together from VHS footage, PNG cards and the voiceover. Colours and type are in
`../theme/` (see its README).

| File | Job |
|---|---|
| `render_cards.sh OUT JOBS` | Renders captions, title cards, end cards, the README header and the wordmark as PNGs with headless Chrome from `cards/card.html`. `cards/jobs.example.tsv` shows the job format. |
| `compose.sh place` | Turns VHS footage into a BT.709 master on the layout canvas (16:9 full frame; 4:5 on a 1080x1350 Ink canvas). |
| `compose.sh caption` | Overlays a caption PNG, optionally for a time window only. |
| `compose.sh still` | Turns a title or end card into a clip. |
| `compose.sh concat` | Joins clips that share size, rate and tags. |
| `compose.sh loudnorm` | Two-pass loudnorm to -14 LUFS / -1.5 dBTP, with a gain + limiter fallback. |
| `compose.sh mux` / `probe` / `frame` | Adds the audio; prints tags, duration and loudness; pulls one frame. |
| `edl.json` | The edit list both cuts are built from: segments of the VHS takes (in, out, time on screen), captions and overlays on the cut's clock, and the pauses set into the voiceover take. |
| `edit.py cards / voice / video / report` | Renders the edit list's cards, builds the voiceover (pauses, then `compose.sh loudnorm`), builds one cut in a single encode, and prints the share of real terminal footage with its time ranges. |
| `grade_opener.py` | Grades the opener clip to the palette: black point to Ink, white point to Paper, the kept thread to Vermilion. |

The two cuts were built this way:

```
python3 grade_opener.py ../opener/gen1.mp4 build/opener.mp4 --report
python3 edit.py cards 16x9 build && python3 edit.py cards 4x5 build
python3 edit.py voice ../audio/vo_take2.mp3 build
python3 edit.py video 16x9 ../takes build/opener.mp4 build build/video_16x9.mp4
bash compose.sh mux build/video_16x9.mp4 build/voice_norm.wav ../../clotho-demo-16x9.mp4
bash compose.sh probe ../../clotho-demo-16x9.mp4
python3 edit.py report 16x9
```

The step-by-step subcommands, for a cut put together by hand:

```
bash render_cards.sh build/cards jobs.tsv
DUR=6 bash compose.sh place takes/s4_fork_16x9.mp4 build/s4.mp4 16x9
bash compose.sh caption build/s4.mp4 build/cards/caption_s4_1920.png build/s4c.mp4 0.5 5.5
bash compose.sh still build/cards/end_1920.png build/end.mp4 16x9 3
bash compose.sh concat build/video.mp4 build/opener.mp4 build/s4c.mp4 build/end.mp4
bash compose.sh loudnorm voice.wav build/voice.wav
bash compose.sh mux build/video.mp4 build/voice.wav out/demo-16x9.mp4
bash compose.sh probe out/demo-16x9.mp4
```

Why it is built this way:

- VHS writes untagged yuv420p at 25 fps whose colours match its own screenshots when decoded as BT.601. A
  player that reads it as BT.709 shifts the red from `#E2462F` to about `#F0542C`. `place` converts once to
  BT.709 and tags the file; the red then measures `#E14630` after decoding.
- Every `scale` uses `accurate_rnd+full_chroma_int`. Without them, 1080-wide frames lost about one code value of
  Ink per encode (`#121212` came back as `#0D0D0D` after two generations).
- The ffmpeg build used here has no `drawtext` or `libass`, so all text is a PNG overlay. Fonts load under
  renamed families, so a missing font shows up as a serif instead of a silent substitute.
- Two-pass `loudnorm` can silently fall back to dynamic mode. It did so in my tests when the gain would push the
  true peak over the target, and also for steady pink noise (measured loudness range 0) with plenty of headroom;
  the same noise with a slow swell went linear. The script reads `normalization_type` from pass 2; if it is not
  `linear`, it applies the gain plus a limiter 1 dB under the target and measures again (up to four rounds,
  aiming for 0.2 LU), and fails if the result is more than 1 LU off or the true peak is over the target.
- Every lavfi colour source is bounded with `-t`; an unbounded one runs forever.

Needs ffmpeg/ffprobe, Python 3 (for reading loudnorm's JSON) and Google Chrome. Fonts: Outfit and Space Grotesk
(OFL) and Dank Mono, read from `FONT_DIR` (default `$HOME/Library/Fonts`).
