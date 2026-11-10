# Clotho: colours, type and terminal theme

In Greek myth three Fates spin, measure and cut the thread of every life. Clotho spins. The look follows
that: black ink, paper, and one red thread for the future you keep.

## Palette: Shears / Vermilion

| Name | Hex | Share | Use |
|---|---|---|---|
| Ink | `#121212` | 60% | backgrounds, terminal background |
| Paper | `#EDE8DF` | 30% | text on dark; light surfaces such as the README header |
| Vermilion | `#E2462F` | 10% | the one red, on dark surfaces |
| Sealing Wax | `#A8231E` | | the same red, on Paper |

Red means "the thread you keep". It is used for nothing else: the kept timeline, the `● keep` marker, the
wordmark's dot, the install line, and one key number. Never `#FF3B30` or any pure, saturated digital red.

Contrast (WCAG 2): Paper on Ink 15.35, Vermilion on Ink 4.59, Sealing Wax on Paper 5.89, Ink on Vermilion
4.59. Vermilion on Paper (3.34) and Sealing Wax on Ink (2.61) fail: each shade stays on its own surface.

## Terminal

The CLI emits named ANSI codes only; the theme supplies the colours.

| What | Codes | Renders as (VHS, measured) |
|---|---|---|
| kept timeline, `● keep`, install line, the one key number | `ESC[31m … ESC[39m` | Vermilion, 4.59:1 |
| unchosen timelines | `ESC[2m … ESC[22m` | Paper at 50% (`#7F7C78`), 4.58:1 |
| IRREVERSIBLE badge | `ESC[22;39;7m ✂ IRREVERSIBLE ESC[27m`, then re-apply `ESC[2m` or `ESC[31m` for the rest of the row | Ink on a Paper box, 15.4:1, full strength inside a dimmed row |
| hierarchy | bold `ESC[1m`, dim, reverse | no other hues |

The `39` in the badge matters: reverse video swaps the current colours, so inside a red row `ESC[22;7m` alone
draws a red box.

Files:
- `clotho-theme.json`: VHS theme. Red and bright red are Vermilion; every other slot is a neutral step
  between Ink and Paper (Lab chroma 5 or less), so nothing but red is ever a hue.
- `clotho-16x9.tape`, `clotho-4x5.tape`: font, size and theme for each cut (`Source` them from a tape).
- `clotho-agg-theme.txt`: the same colours for `agg --theme` (background, foreground, then the 16 ANSI colours).
- `ansi256.json`: nearest xterm-256 codes. Ink 233, Paper 254, dim 244, Vermilion **167** (`#D75F5F`, 5.08:1
  on 233). 196 and 160 are nearer to Vermilion but are pure saturated reds, so they are skipped; 167 reads
  slightly pinker than Vermilion.

Glyphs in Dank Mono: `●`, `·` and `→` are Dank Mono's own. `✂` is not in Dank Mono: Chrome draws it from
Zapf Dingbats, in one colour, and it is almost two cells wide, so it touches the next letter. Box-drawing
characters (`├ └ ─`) are drawn by the terminal itself.

## Type

| Role | Font | Notes |
|---|---|---|
| Terminal | Dank Mono | 52 px at 1920x1080 (10.6 pt on a 390 pt wide phone), 30 px in the 4:5 cut (10.8 pt) |
| Wordmark | Space Grotesk 500, tracking -4% | `clotho.` lowercase; dot Vermilion on dark, Sealing Wax on light |
| Captions, body | Outfit 400 | |

`wordmark-dark.svg` / `wordmark-light.svg` are outlined (no font needed); the PNGs are 1600x421 on a
transparent background.

## Captions and cards

Phone sizes assume the video shown 390 pt wide (16:9: scale 0.203; 4:5: scale 0.361).

| | 1920x1080 | 1080x1350 |
|---|---|---|
| Caption | Outfit 400, 64 px (13.0 pt), tracking -5%, Paper on an 82% Ink scrim, bottom 120 px | 52 px (18.8 pt), up to 2 lines, max 952 px, bottom 200 px |
| Section title | Space Grotesk 500, 132 px (26.8 pt), -4%, Paper on Ink | 96 px (34.7 pt) |
| End card | wordmark 220 px; tagline Outfit 64 px (13.0 pt); install line Dank Mono 46 px (9.3 pt) Ink on Vermilion; repo 48 px; @NousResearch line 44 px | wordmark 200 px; tagline 56 px (20.2 pt); install 27 px (9.7 pt); repo 38 px; @NousResearch 34 px |
| Terminal rows free for a caption | keep output above y = 860 (12 rows + prompt) | the terminal sits above the caption zone |

Caption text on the scrim: 15.35:1 over Ink, 9.48:1 over a Paper-white frame. No red in captions.

`end-card-16x9.png`, `end-card-4x5.png` and `readme-header.png` are reference renders from
`../compose/cards/card.html`.
