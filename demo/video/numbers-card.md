# Evidence card (beat B8, 63.8–71.8 s)

One card, four stat tiles, built as a PNG with headless Chrome (the same route as the captions; this ffmpeg has no
drawtext). Form chosen with the dataviz method: a handful of headline numbers is a **KPI row of stat tiles**, not a
chart; there is no series, so there is no categorical palette, no legend and nothing for the palette validator to
pass (its own scope note: categorical palettes only; run on Vermilion and Paper it passes contrast and CVD and
"fails" lightness band and chroma floor for Paper, which is a text colour, not a series colour). The voiceover reads
the claim behind the numbers, which stands in for a table view.

## The numbers (all measured; nothing rounded into a pass)

Source of every value: `docs/EVAL.md` section 3 (the results table, column "all"), which rolls up the five
`eval/results/<timestamp>/headline.json` files named there: scripted `20261110-104056`, deepseek-v4-flash
`20261110-105118`, deepseek-v4-pro `20261110-111747`, Claude `20261110-112333` and `20261110-112440`. Re-read on
2026-11-10 for this revision: unchanged.

| # | Number | Label (on the tile) | Exact source | What it does not say |
|---|---|---|---|---|
| 1 | `0` | escaped effects | EVAL.md §3 rows "escapes: HTTP sink requests / SMTP sink messages / workspace changed / sentinel folder changed", all 0 in every column; headline.json `escapes.sink_new`, `smtp_new`, `workspace_changed`, `sentinel_changed` = 0 in all five files | The same table counts 1 Hermes state file (`cron/dispatch_held.json`, pro t08), written by Hermes' own idle gateway ticker (EVAL.md note E). The README and EVAL.md name it. |
| 2 | `72/72` | applied exactly | EVAL.md §3 rows "sink exact after Apply", "SMTP exact after Apply", "workspace = pre-run + applied": 72/72 each | "every Apply check incl. post" is 71/72 (flash t11 r3, note M: the model claimed a skill it never attempted). Not on the tile; the caption says 69 of 72 runs passed every check. |
| 3 | `0` | model calls in Apply | EVAL.md §3 row "model calls during Apply (4 ways)": 0 in every column; headline.json `apply_model_calls_4_instruments` = 0 | |
| 4 | `1.5 s` | Apply time, 33-run median | EVAL.md §3 row "Apply wall s, median (max)", deepseek-v4-flash column: 1.50 (max 2.84); headline.json `apply_wall_s.median` = 1.5 for `20261110-105118` | Wall time of the whole `hermes clotho apply` command over the 33 deepseek-v4-flash runs, Hermes start-up included (time inside the command: median 0.25 s). The page has no median over all 72 runs, so the label states the tile's own scope (33 runs) instead of borrowing the title's. The demo's own Apply line shows 0.1 s for its timeline. |

Card title: **Measured: 72 task runs, 11 tasks** (EVAL.md §1 "Runs": scripted 11×3, flash 11×3, pro 4×1, Claude
2×1 = 72; tasks t01–t11).

Proof lines (on the card, under the tiles; they replace the earlier footnote, which measured 5.7 pt on a phone):
the proof points that are not already a tile, in the order the README gives them: *Fails closed · The shell
actually runs, in a sandbox · Drift stopped Apply 14/14 · Forks compares full effect ledgers across models · Try it:
`hermes clotho demo`*. Sources: `T3FailClosed` (a fault injected into the plugin blocks the call), the sandbox
tests named in the README, EVAL.md §3 row "drift stopped" (14/14, column "all"), the README's Forks section. Outfit
400 in Paper, the separators in Paper at 45%, the command in Dank Mono; no red. 16:9: 48 px (9.7 pt on a phone),
two set lines; 4:5: 34 px (12.3 pt), three set lines. What "escaped" counts is said by the voiceover ("not one
request, email or workspace change got out"); the one Hermes housekeeping file (EVAL.md note E) is named in the
README's Measured section and in EVAL.md, no longer on the card.

Caption under the card (burned in, beat B8): **69 of 72 runs passed every check · the 3 misses are listed in
EVAL.md** (EVAL.md §3 "task runs passed 69/72" and "The three failing runs and what they show").

Formatting: integers without separators; `72/72` with no spaces; time to one decimal with a thin space (U+2009)
before `s`. **Proportional figures**, not `tnum`: the dataviz anti-patterns list `tabular-nums` on a large
standalone number as a mistake (equal-width digits look loose at display size).

## Look: palette "Shears / Vermilion", one number in red

The video's red means "the thread you keep", and the brand allows it on "one key number". On this card that is
**tile 3, the `0` of "model calls in Apply"**, the same number the terminal receipt shows in red in B4 and B5
(Apply keeps the chosen future without asking a model again). The other three numbers are Paper. This is the
dataviz "highlight one, grey the rest" emphasis, done with the brand's one accent; nothing else on the card is red,
and nothing on it is any other hue. Which number is red may change; two red numbers are never allowed.

| Element | Colour | Contrast (WCAG formula, on Ink) |
|---|---|---|
| Canvas and tile fill | Ink `#121212` (tiles are **not** lifted: Vermilion on a lifted `#1A1A1A` falls to 4.27:1) | |
| Tile edge | 1 px hairline, Paper at 14% (about `#31302F`, 1.42:1) | decorative |
| Numbers 1, 2, 4 | Paper `#EDE8DF` | 15.35:1 |
| Number 3 (`0`, model calls in Apply) | Vermilion `#E2462F` | 4.59:1 (and display size) |
| Labels | Paper at 66% (`#A39F99`) | 7.12:1 |
| Proof lines | Paper; separators Paper at 45% | 15.35:1 |
| Title | Paper | 15.35:1 |
| Caption scrim | Ink at 82%, caption text Paper (as every caption) | |

Never: `#FF3B30` or any saturated digital red, Sealing Wax (that shade is for light surfaces; this card is dark),
a red label, a red hairline, a red tile, a glow.

Type (fonts as the brand note gives them; the dataviz rule "the hero figure uses the same sans as everything else"
decides the numbers):
- Numbers: **Outfit 500**, tracking −0.02em, proportional figures. *Changed from the first draft, which set them in
  Space Grotesk: Space Grotesk is the wordmark's face; on a card whose every other line is Outfit, a second face on
  the numbers is the "display face on the hero figure" anti-pattern.* Space Grotesk stays the fallback if the numbers look weak.
- Label: Outfit 400, tracking −0.02em, at most 2 lines, under the number.
- Title: Outfit 400, tracking −0.02em (cards and captions share the open tracking choice in `script.md`).
  Proof lines: Outfit 400, the command in Dank Mono.
- Text wears text colours only; the one red is the brand's "key number", not a status or series colour, so no icon
  or legend is needed.
- Fonts load under renamed families, so a missing file shows as a visible serif instead of silently using an
  installed copy. Check with `--dump-dom` before compositing.

### 1920 × 1080 (16:9)

```
y   72 ┌ safe margin ───────────────────────────────────────────────────────────┐
y  200   Measured: 72 task runs, 11 tasks                (title, 56 px, x 120)
y  360 ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐      one row of 4 tiles
       │   0    │ │ 72/72  │ │   0    │ │ 1.5 s  │      number 160 px; tile 3's 0 in Vermilion
       │escaped │ │applied │ │model   │ │Apply   │      label 48 px, 2 lines max
       │effects │ │exactly │ │calls in│ │time,   │
       │        │ │        │ │Apply   │ │median  │
y  720 └────────┘ └────────┘ └────────┘ └────────┘      tile 390 x 360, gap 40, side margins 120
y  622   proof lines, 48 px, 2 set lines (tiles moved up: title y 110, tiles y 236-596)
y  880   [ caption scrim: 69 of 72 runs passed every check · ... ]   bottom edge 120 px above the frame
```
At 16:9 the label is 48 px (9.7 pt on a phone: accepted because the numbers carry the card); the proof lines are
the same size.

### 1080 × 1350 (4:5)

```
y   96   Measured: 72 task runs,                  (title, 52 px, 2 lines, x 64)
         11 tasks
y  290 ┌──────────────┐ ┌──────────────┐          2 x 2 grid, tiles 460 x 400, gap 32, margins 64
       │      0       │ │    72/72     │          number 150 px
       │escaped       │ │applied       │          label 44 px
       │effects       │ │exactly       │
y  690 └──────────────┘ └──────────────┘
y  722 ┌──────────────┐ ┌──────────────┐
       │      0       │ │    1.5 s     │          tile 3's 0 in Vermilion
       │model calls   │ │Apply time,   │
       │in Apply      │ │median        │
y 1122 └──────────────┘ └──────────────┘
y  808   proof lines 34 px, 3 set lines, end about y 940 (as built: title y 56 in one line, tiles 460 x 300
         from y 152, 32 apart; the caption box starts at y 1002)
         [ caption: 69 of 72 runs passed every check ... ]    bottom 200 px above the frame
```
The grid sits high to leave room for the title and the proof lines above the 200 px caption zone. `72/72` at 150 px is
about 4 em wide in Outfit (estimate, about 600 px): **likely too wide for a 380 px text box**. Measure it in the
render; if it overflows, set that tile's number at 112 px, or let the `/72` drop to the label size. Same check for
`1.5 s` (narrower, should fit).

## Motion (in the edit)
Tiles fade up left to right, 120 ms apart, 240 ms each, no counting animation (never fake a counter). Hold the full
card at least 6 s.

## Checks before it goes into the cut
1. `--dump-dom` shows both renamed font families `loaded`.
2. Sample the rendered PNG: exactly one region of Vermilion (tile 3's number); background `#121212`; no other hue.
   Measure the contrast of the four text colours on the PNG.
3. Look at it at phone size (390 pt wide) next to the frame from the cut; numbers must read without zoom.
4. Every number on the card equals the row named above; re-read EVAL.md before rendering, in case a later
   measurement replaced it.
