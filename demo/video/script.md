# Clotho demo video: script and shot plan

Two cuts from one plan: **16:9 at 1920×1080** and **4:5 at 1080×1350** (the X feed). Target length **about 79 s**
(limit 88). Voiceover: `voiceover.txt`, **1,146 characters** by `wc -m` (limit 1,150; it is recorded, so that text is
final). Captions carry the whole story with the sound off. Masters:
`demo/clotho-demo-16x9.mp4` and `demo/clotho-demo-4x5.mp4`.

Rules this plan follows, and where they come from:
- Video rules: length, phone-legible captions, −14 LUFS ±1, no dead air over 1.5 s, real plugin footage at
  least 60% of the runtime, two cuts, never a faked counter, real numbers from the actual run.
- The hook is the fork moment: three futures side by side, pick the safe one, apply. An early outline had one
  timeline delete 40 files; **no timeline in the recording deleted 40 files**, so the hook uses the real counts:
  29, 51 and 32 deletes; two timelines lost the precious `resume_v3_final.docx`, the Claude timeline lost none.
- The name and brand note, quoted where it is binding:
  - Story: "in Greek myth the three Fates spin, measure and cut the thread of every life. Nous named its RL
    framework Atropos, the Fate who cuts. Clotho is her sister: she **spins**. This plugin spins every possible
    future of an agent task (rehearsal + Forks), lets you measure them, and keeps only the one you choose."
    Tagline: **"Spin every future. Keep one."**
  - "Red means "the thread you keep" (the red thread of fate)." Red is used "only [for] the KEPT timeline/thread,
    the "● keep" marker, the wordmark's dot, the install CTA, one key number". "Never use #FF3B30 or any
    pure-hue/saturated digital red." "The IRREVERSIBLE badge is NOT red: "✂ IRREVERSIBLE" in an inverted box (Ink
    text on Paper), always at full opacity even inside a dimmed timeline row."
  - Fonts: Dank Mono (terminal), Space Grotesk 500 (the wordmark "clotho." lowercase), Outfit (captions, cards).
- Decisions: never claim "first" or "only", never name another project's pull request (the claim is "compare full
  effect ledgers, including captured messages and web requests, and apply the chosen one with 0 model calls");
  the hook is the real mixed-model fork and `demo` replays recorded turns and says so on screen; the demo applies
  the timeline that loses no precious file, and its summary sits in a command the plugin could not preview, so
  **no landing request is shown for the fork**; one voiceover call, at most 1,150 characters; the terminal colour
  contract in "Colour on screen" below.
- Brand files written with this plan (not by it): `theme/clotho-theme.json` (the VHS theme the tapes load),
  `theme/ansi256.json` (nearest 256-colour codes), and the wordmark and card specs in `theme/`. Text in the video is
  PNG overlays rendered with headless Chrome (this ffmpeg has no drawtext); VHS writes untagged BT.601, so every
  master is re-encoded with BT.709 tags.

## How the name is said

The voice says **KLOH-thoh**. `voiceover.txt` spells it **Clotho** (twice), plainly: in a test reading the plain
spelling came out as one word, "Clotho", and the phonetic "Kloh-thoh" as two separate syllables. Captions, title
and cards spell it **Clotho** / **clotho.** as always. After the take, transcribe it and listen to both names:
"Clotho" and "Atropos" (AT-ruh-pos). "Nous" should come out as "noose".

## Beat sheet

Times are targets; the edit snaps every beat to the word timings of the chosen voiceover take (transcribe it).
"Real" = terminal footage of the plugin's own output. Captions: Outfit 400, Paper `#EDE8DF` on an Ink `#121212`
82% scrim; emphasis by weight 500, never by colour; **never red** (see "Colour on screen"). Caption sizes and
positions: 64 px (16:9) / 52 px (4:5), bottom edge 120 px / 200 px above the frame.

| Beat | Time (s) | Footage (shot · source) | Voiceover (from voiceover.txt) | Caption (sound off) |
|---|---|---|---|---|
| **B1 hook** | 0.0–5.2 | Real · S1 `s1_hook`: the "what each timeline would do to the precious files" block of `hermes clotho demo`, cut in on the finished frame (no typing). The kept timeline (claude-sonnet) is the red thread; the other two are dim. In the edit, a 2 px Paper outline lands on each dim "precious lost" line as the caption names it. | Three models cleaned up one folder. Two deleted the résumé. None of it happened. | 0.0–1.4 **3 models, 1 messy Downloads folder** · 1.4–2.8 **2 of them deleted resume_v3_final.docx** · 2.8–5.2 **None of it happened.** (Context, Lean, Snapback all on screen by 2.8 s) |
| **B2 the Fates** | 5.2–10.4 | Opener clip (`opener.md`, 4 s): one thread spins off a spindle and splits in three; two fade, one stays vermilion. 8.4–10.4: the wordmark **clotho.** (dot in Vermilion) and the tagline fade in over the clip's held last frame. | In Greek myth, three Fates spin, measure and cut the thread of every life. | 5.2–8.4 **In Greek myth, three Fates spin, measure and cut the thread of every life.** · 8.4–10.4 the title is the caption: **clotho.** / **Spin every future. Keep one.** |
| **B3 she spins** | 10.4–20.4 | Real · S2 `s2_run`: take A, `hermes clotho run "..."` typed and its first lines, ending on "nothing will happen for real". The name is typed on screen while the voice says it. | Nous named its RL framework Atropos, the Fate who cuts. Clotho is her sister. She spins. My plugin for the Hermes Agent spins every possible future of an agent task, | 10.4–13.6 **Nous named its RL framework Atropos: the Fate who cuts.** · 13.6–16.4 **Clotho is her sister. She spins.** · 16.4–20.4 **A Hermes Agent plugin that spins every possible future of an agent task,** |
| **B4 one run** | 20.4–37.0 | Real · S3 `s3_report_apply`: `hermes clotho report \| less` (two pages, the POST row with its "✂ IRREVERSIBLE" badge), `hermes clotho apply --except N` (the delete), the receipt with its red "0 model calls", `sink`, `ls`. The jump cut from S2 to S3 is marked by the caption change. | lets you measure them, and keeps only the one you choose. Every side effect goes into a ledger instead of happening: files, commands, messages, web requests, cron jobs. Untick what you don't want. Apply replays exactly what you kept, with zero model calls. | 20.4–24.0 **lets you measure them, and keeps only the one you choose.** · 24.0–28.0 **Every side effect goes into a ledger. Nothing happens yet.** · 28.0–31.0 **Untick the delete. Keep the rest.** · 31.0–34.6 **Apply: only what you kept · 0 model calls** · 34.6–37.0 **The webhook got the summary. scratch.tmp is still there.** |
| **B5 fork** | 37.0–54.0 | Real · S4 `s4_fork`: `hermes clotho demo --dir demo` live (typed, then about 3 s of its scroll; jump cut), then held pages of that same output: header, comparison, differences, the precious block, the apply block, the folder afterwards. | Fork spins the same task on several models at once. Compare the futures and apply only the one you keep. Here, the Claude timeline kept all ten precious files. The demo replays that fork in about ten seconds, no key needed. | 37.0–40.0 **Fork: same task, 3 models, 3 futures at once** · 40.0–43.5 **A real fork, recorded and replayed: no key, nothing leaves the machine** · 43.5–46.5 **The ledgers, compared: 56 differences** · 46.5–50.0 **Deletes: 29 · 51 · 32. Precious files lost: 1 · 1 · 0** · 50.0–52.5 **Keep the one that lost nothing: 130 effects, 0 model calls** · 52.5–54.0 **10 of 10 precious files kept** |
| **B6 drift** | 54.0–59.0 | Real · S5 `s5_drift`: take A restored to before Apply; `echo ... >> todo.md`; `hermes clotho apply` stops and names `todo.md`; `echo "exit $?"` prints 4. | If a file changes after the run, apply stops before touching anything. | 54.0–56.5 **Changed a file by hand? Apply stops before it changes anything.** · 56.5–59.0 **Drift stopped 14 of 14 times in my tests** |
| **B7 cron** | 59.0–63.8 | Real · S6 `s6_cron`: take C, `hermes clotho cron daily-digest --runs 2`, held on the roll-up ("job state: untouched"). Fallback if take C is not recorded: a card quoting the README's real cron roll-up (that card is not footage). | Cron mode spins a scheduled job before it runs on its own. | **cron: spin a scheduled job before it runs on its own. Delivery held, job state untouched.** |
| **B8 evidence** | 63.8–71.8 | Card · `numbers-card.md`. | In seventy-two task runs, not one request, email or workspace change got out, and every apply landed exactly. | **69 of 72 runs passed every check · the 3 misses are listed in EVAL.md** |
| **B9 close** | 71.8–79.0 | Card over the opener clip's last 2 s, then its held last frame (`opener.md`). | Clotho is open source, MIT licensed. Spin every future. Keep one. Thanks, Nous Research. | wordmark **clotho.** · **Spin every future. Keep one.** · install line `hermes plugins install NicWeinbrecht/hermes-clotho --enable` (the red CTA) · `github.com/NicWeinbrecht/hermes-clotho` · **for the Hermes Agent · @NousResearch** |

**Real footage:** B1 5.2 + B3 10.0 + B4 16.6 + B5 17.0 + B6 5.0 + B7 4.8 = **58.6 s of 79.0 s = 74.2%**. Without
the cron take (B7 as a card): 53.8 s = **68.1%**. Both above 60%. Not footage: the opener and title (5.2 s), the
evidence card (8.0 s), the close (7.2 s). The story beat costs no footage share: only its first sentence plays over
the opener; the rest plays over take A, with the name typed on screen as it is said.

**Dead air:** 198 words at about 2.75 words a second is about 72 s of speech, plus paragraph pauses, for about
78 s. Every gap stays at or under 1.2 s by placing each sentence on its own footage moment (B4 has four sentences
over 16.6 s; B5's sentences follow the pages). The tightest spot is B7 → B8 (cron's line is about 3.6 s in a
4.8 s beat): start the card on the next word, not on a fixed time. The close ends 1.5 s after the last word; if
the measured gap there is over 1.5 s, trim the held frame, not the words.

## The story, and where it sits

The myth comes **after** the hook, never before it: the first 3 s must be the fork moment and must
carry context in the first five words (hook framework). Placement of the story's own words, beat by beat:
- "the three Fates spin, measure and cut the thread of every life" → B2, over the opener's spinning thread.
- "Nous named its RL framework Atropos, the Fate who cuts. Clotho is her sister: she spins." → B3, over take A.
- "spins every possible future of an agent task (rehearsal + Forks), lets you measure them, and keeps only the
  one you choose" → B3 into B4: the single run is one spun future, its report is the measuring, Apply keeps it;
  B5 spins three at once and keeps one.
- "Spin every future. Keep one." → the title in B2, and said and shown again in B9.

The hook's snapback ("None of it happened.") hands straight to the myth: none of it happened because those were
threads being spun, not cut.

## Colour on screen

Palette "Shears / Vermilion": Ink `#121212` (canvas, terminal background, scrims), Paper `#EDE8DF` (all text),
Vermilion `#E2462F` (the one red on dark). Sealing Wax `#A8231E` is the same red for light surfaces; the video has
no light surface, so it does not appear. Contrast (WCAG formula): Paper on Ink 15.35:1; Vermilion on Ink 4.59:1;
Ink on Vermilion (the install CTA) 4.59:1; terminal dim (xterm.js draws SGR 2 at 50%: `#807D78`) 4.57:1 on Ink.

**Red appears only here** (nothing else in the video is red, and nothing here is any other hue):

| Where | What | Source of the colour |
|---|---|---|
| B1, B5 | the kept timeline (claude-sonnet) and its "● keep" marker | the plugin's own output (ANSI red, theme Vermilion) |
| B4, B5 | the receipt's "0 model calls" (the one key number) | the plugin's own output |
| B2, B9 | the dot of the wordmark **clotho.** | title PNG |
| B9 | the install line `hermes plugins install NicWeinbrecht/hermes-clotho --enable` (Ink text on a Vermilion fill, as in the brand reference render S1) | close PNG |
| B8 | the "0 model calls in Apply" tile: the same key number | card PNG (`numbers-card.md`) |

Never red: captions, outlines (Paper), the IRREVERSIBLE badge (reverse video, Ink on Paper, full opacity even in a
dim row), drift and blocked messages, errors, the prompt (bold Paper). Unchosen timelines are dim, not grey-hued.
The opener's red thread is graded to Vermilion; a pure-hue or saturated digital red (anything near `#FF3B30`) in
any frame is a reject. The edit never paints red onto terminal footage: if the plugin's output does not show a
colour the table expects, the frame is real and stays as it is, and the gap is reported.

## Every number on screen or in the voiceover, and its source

The demo numbers are fixed by the recording (`demo/recordings/`, replayed byte for byte); I checked them on five
runs of the demo command: identical counts every time. "Real run output" means the frame shows it.

| Number | Where | Source |
|---|---|---|
| 3 models (deepseek-v4-pro, kimi-k2.6, Claude sonnet) | B1, B5 caption, VO | demo output header; `demo/recordings/manifest.json`; README.md "Demo" |
| 2 of 3 lost `resume_v3_final.docx`; Claude lost none ("1 · 1 · 0") | B1, B5 caption, VO "two deleted the résumé" | demo output "precious lost: …"; README.md "What the recorded fork did" |
| deletes 29 · 51 · 32 | B5 caption (and B1 frame) | demo output "deletes 29 files / 51 / 32"; README.md "What the recorded fork did" |
| 56 differences | B5 caption | demo output "differences (56)" (same on every replay) |
| 130 effects applied, 0 model calls | B5 caption | demo output "applied 130/131 · skipped 1 · 0 model calls · 0.1 s" (the skipped one is the summary command it could not preview) |
| 10 of 10 precious files kept; "all ten" | B5 caption, VO | demo output "precious files kept: 10 of 10" |
| about ten seconds, no key | VO B5 | README.md "Demo" ("about 10 s on an Apple-silicon Mac; no API key, no network"); timed runs 8.84–12.2 s |
| 0 model calls (single run) | B4 caption, VO | take A's own receipt line on screen; EVAL.md §3 "model calls during Apply (4 ways)": 0 |
| exit 4 on drift | B6 frame | README.md "Apply" exit codes (4 drift); the frame shows it |
| 14 of 14 drift stops | B6 caption | EVAL.md §3 "drift stopped 14/14" |
| 2 runs | B7 frame | take C's own output |
| 72 task runs, 11 tasks; 0 escaped; 72/72 exact; 0 model calls; 1.5 s; 69/72 | B8 card, caption, VO "seventy-two", "not one request, email or workspace change got out", "every apply landed exactly" | EVAL.md §3 and §1 "Runs"; `eval/results/*/headline.json` (see `numbers-card.md` for each row) |
| MIT | VO B9 | README.md last line ("MIT licence") |

Not used anywhere: "40 files" (never happened), "first" or "only", any speed claim about forks. The brand
reference render's timeline numbers (412 deletes, 38 moves) are placeholders and appear nowhere.

## Hook (hook-framework: Context → Lean → Snapback, all within 3 s on screen)

Chosen, **lean: shock** (re-checked against the framework after the rename; unchanged, because the name and the
story must not displace the fork moment):
- Context (0.0 s): "3 models, 1 messy Downloads folder" over the three timeline blocks: what this is about, in
  five words, said, shown and written at once. Check: context inside the first five words of the voice ("Three
  models cleaned up one folder").
- Lean (1.4 s): "2 of them deleted resume_v3_final.docx", with the two dim "precious lost" lines outlined.
  Check: a clear lean, shock.
- Snapback (2.8 s): "None of it happened." It pulls the viewer from "agents are dangerous" to "these are
  futures you can pick from", which is the product; the title 2.4 s later names it ("Spin every future. Keep
  one."). Check: it reverses the direction of the lean.

Other options considered:
1. *Shock, with the tagline:* "Same task, three futures. / Two of them delete the résumé. / I keep the third.
   The other two never happen." (Strong, and it says "keep one" in the hook, but "keep the third" before the viewer
   knows what keeping means slows the snapback; it is the B9 line instead.)
2. *Proof:* "51 deletes. 29. 32. Same task, three models. / One kept every precious file. / And nothing was
   deleted at all."
3. *Pain:* "Your agent is about to tidy your Downloads. You haven't read a thing. / Here are three ways it could
   go. / None of them has happened yet."
4. *Shock:* "This model deleted the résumé. So did this one. / Neither deletion happened."
5. *Myth first (rejected):* "In Greek myth, one Fate cuts the thread of every life. / Mine spins it instead."
   Context arrives late (no product, no folder, no fork in the first 3 s) and it is not the fork moment.

"Three futures side by side": a true side-by-side does not fit. Three columns of the real 58-column output on
1920 pixels would need a cell of about 11 px (about 2 pt on a phone). The hook shows the three timelines
stacked, which is how the plugin prints them; the red kept thread and the outlines do the comparing.

## Shots, takes and what needs a live model

| Shot | Tapes | Model calls | How it is made |
|---|---|---|---|
| S1 hook | `tapes/s1_hook_{16x9,4x5}.tape` | none | held page of the S4 output of the same layout (render S4 first) |
| S2 run | `tapes/s2_run_*.tape` | **live at take time (take A)** | `asciinema play` of `casts/take_a.cast`, recorded once by `render.sh take-a`, so both layouts show the same run |
| S3 report + Apply | `tapes/s3_report_apply_*.tape` | none | take A's workspace and run restored from a snapshot, then `report`, `apply --except N`, `sink`, `ls` typed live |
| S4 fork | `tapes/s4_fork_*.tape` | none (recorded turns, no key) | `hermes clotho demo` typed live, its output saved with `tee`, then held pages of that same output |
| S5 drift | `tapes/s5_drift_*.tape` | none | take A restored to before Apply, one hand edit, `apply` typed live |
| S6 cron | `tapes/s6_cron_*.tape` | **live at take time (take C)** | `asciinema play` of `casts/take_cron.cast` |

The `.tape` files are generated by `tapes/make_tapes.sh`, which reads the theme from `theme/clotho-theme.json`
(VHS takes a theme only inline); `render.sh shots` runs it before every render, so a tape never carries a stale
theme. `render.sh` reads the plugin's folder name from `names.py` and refuses to run until the command name there
is `clotho`.

Live calls in total: take A, at most 3 attempts × about 5 calls; take C, at most 2 attempts × 2 runs × about 4
calls; all `deepseek/deepseek-v4-flash` on OpenRouter. Cost estimate: about USD 0.005–0.006 per run (EVAL.md §3,
flash "cost per task run" 0.0061 including a plugin-off run; README.md's sample report shows $0.0052), so at most
about USD 0.04. Meter before and after each take (`CL_METER`); the takes refuse to start without it.

### Take A acceptance (`render.sh take-a` checks it)
The take is kept only if its ledger has exactly one `delete scratch.tmp` row and one `POST …/team` web request
row. If the model chains the curl into a bigger command (Claude did that in a live fork), the POST shows as a
command it could not preview and the sink beat would not work: retake, at most 3 takes, then fall back to
showing the receipt without the sink line and drop "The webhook got the summary" from the caption.

### Paths on screen
Every path on screen sits under one short neutral folder, `CL_STAGE` (default `/tmp/cl`, shown by macOS as
`/private/tmp/cl/...`, the same length as before, so no measured line width changes). `render.sh setup` gives it
group `staff`. Reason, measured: the demo checks that replayed tool results match the recording's, and `ls -l`
prints the group; a folder of group `wheel` (plain `/tmp`, the macOS shared users folder) gave **"13 of 20"** on screen, a
`staff` folder **"20 of 20"**. Nothing on screen may show a home folder name, a user name or the scratch Hermes
home: the scratch home is only reached through the `hermes` shell function in `tapes/env.sh`.

### Terminal sizes (measured in the first render)
- 16:9: VHS 1600×900, Dank Mono 44 px, padding 48 → **14 rows × 60 columns**; placed at x 160, y 0 on the
  1920×1080 Ink canvas, so the caption scrim (bottom edge 120 px above the frame) sits under the last row.
- 4:5: VHS 1080×1000, Dank Mono 30 px, padding 30 → **24 rows × 58 columns**; placed at x 0, y 0 on 1080×1350;
  captions below, bottom edge 200 px above the frame. Padding 40 gave only 57 columns (a 58-column rule wrapped).
- Held pages never exceed the rows (the page helper prints no trailing newline and hides the cursor), so a held
  frame never scrolls. Pages are contiguous slices of the real output, never reworded or reordered.
- VHS writes 25 fps whatever `Set Framerate` says, and untagged BT.601: re-encode with BT.709 tags.

### Lines that depend on output not seen yet (re-check after the code rename and the output freeze)
Nothing has been rendered under the new name: the code still printed the earlier name when this plan was written.
1. **Every line that prints the product name** now prints "Clotho" (expected, not seen): the demo header
   ("── Clotho demo ──…"), the fork table title ("Clotho fork  f-…"), the run banner in S2 ("Clotho r-…: … (nothing
   will happen for real)"), the fork progress line in S4's live scroll (about 115 characters: it wraps, but only in
   the scrolling part, not in a held page), the apply page's "(their changes stay in their own … folders)" and the
   sink note "… could not preview". The tapes' page patterns match any one-word name, so they find these lines
   either way; but a shorter name can re-wrap a line, which moves the end of a held page. Look at every held page
   after the rename.
2. **Colours**: the hook and the fork pages expect the kept timeline in red with the "● keep" marker, the others
   dim, the badge in reverse video at full opacity inside a dim row, and the receipt's "0 model calls" in red. In
   particular, the precious block (S1) is printed **before** the demo's Apply: if the code colours the kept
   timeline only after Apply, the hook has no red thread. Report that; do not paint it in.
3. S5: the drift line's wording ("`<path>` changed since …") is apply.py / report.py output. The tape waits for the
   prompt, not for the words; the caption does not quote them.
4. S3: `@EXCEPT@` (the number of the delete row) and the page breaks depend on take A; `render.sh` fills the
   number from the ledger.
5. S4: the apply block ("applied 130/131 · skipped 1 · 0 model calls · 0.1 s") would change if the demo's
   handling of folder creations changes; the Claude timeline has no folder rows, so it should not.
6. S6: the cron roll-up's wording (take C, not recorded yet).
7. The installed plugin needs `demo/` inside it; `render.sh setup` copies it into `plugins/<name>/demo/` (name from
   `names.py`), because the test installer copies top-level files only.

## Assembly notes
- Masters: H.264 yuv420p BT.709 with explicit tags (`setparams` step), both cuts ≤ 88 s and ≤ 512 MB; loudness
  −14 LUFS ±1 measured after the mix (two-pass `loudnorm` can silently fall back to dynamic mode: read
  `normalization_type`, or use gain plus a limiter and measure again).
- Captions, title and close: PNG overlays from headless Chrome, one PNG per caption per layout; fonts loaded under
  renamed families so a missing font shows as a serif. Wordmark and card geometry: the specs in `theme/`.
- Caption tracking: −5% as first written, or −2% at caption sizes (reads clearer at 52–64 px): open.
- Check after export: every caption legible in a 390-pt-wide phone preview; contact sheet of 3 frames per cut;
  duration; loudness; silence gaps (`silencedetect`); footage share from the edit list; one frame per beat
  sampled for red pixels: red only where "Colour on screen" lists it.
