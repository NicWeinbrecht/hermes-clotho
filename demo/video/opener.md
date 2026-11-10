# Opener and closer clip (one generated clip, at most two generations)

The direction, quoted as given and binding:

> "a single thread spinning off a dark spindle (ink on black, paper-white highlights) and splitting into three
> threads; two fade to grey and one stays vermilion red; optionally a pair of shears cutting a faded thread.
> Muted, tactile, film-like; no glossy neon. No generated text. Wordmark overlaid with ffmpeg."

Where it sits: **not** at 0 s. The first 5.2 s are the hook on real footage (the triple hook lands before any
branding). The clip plays in beat B2 (5.2–9.2 s) under "In Greek myth, three Fates spin, measure and cut the
thread of every life", and its held last frame carries the wordmark and tagline to 10.4 s. In beat B9 (the close)
its last 2 s play again forward, then its last frame holds under the install card. It is **not** played in reverse
for the close: three threads re-joining reads as a rewind, the opposite of "keep one".

Text is never generated. The wordmark **clotho.** (Space Grotesk 500, tracking about −4%, Paper with the dot in
Vermilion `#E2462F`), the tagline, the install line, the repo and @NousResearch are PNG overlays (headless Chrome)
composited with ffmpeg; geometry from the wordmark and card specs in `theme/`.

Caps: at most 350 credits for the whole video, two generations at most for this clip; read the balance before
and after.

## Plan: generation 1 is required, generation 2 only if needed
| # | What | When it runs |
|---|---|---|
| 1 | The prompt below, 16:9, 4 s (2 s is the floor if the model's lengths force it) | always |
| 2 | The same prompt once more, or with the optional shears clause (below) | only if generation 1 is unusable (letter-like shapes, a fourth thread, a saturated or glowing red, a moving camera that ends off-centre, or nothing visibly splits), or if the shears version is wanted |

## Framing that makes one clip serve both cuts

The 4:5 cut is a centre crop of the same clip: 864×1080 out of 1920×1080, scaled 1.25× to 1080×1350. So the
prompt keeps **all action inside the centre 40% of the width**: the spindle at top centre, the thread falling
straight down the middle, the split at the frame's centre, and the three threads fanning only slightly as they
fall to the bottom edge. The left and right thirds stay empty, near-black negative space (in 16:9 that is
calm space; in 4:5 it is cropped away). The wordmark goes in the lower third, centred, over the near-black under
the threads' ends, in both cuts. A soft, grainy, dark frame survives the 1.25× upscale; if the crop looks soft on
a phone, generation 2 becomes a native 4:5 render of the same prompt, never a third generation.

## Before any generation
1. Read the Higgsfield balance and note it. Other generations can draw on the same balance:
   attribute this spend by the generation ids in `transactions`, not by before/after alone.
2. `get_workflow_instructions` (no argument) and `models_explore(action:'recommend')` for "4 s cinematic macro,
   locked-off camera, very slow motion, dark, no text". Pick the model they recommend for a steady, calm frame and
   fine physical detail (thread fibres). Re-read its price.
3. Text-to-video at 1920×1080 (or the model's nearest 16:9), 4 s, 24 or 25 fps, no audio.

## Prompt (generation 1)
```
Cinematic macro shot on 35mm film, locked-off camera, very slow and calm, 4 seconds. Near-total darkness: a matte
black background (#121212) with no texture. At the top centre of the frame, a dark wooden drop spindle, black on
black, turns slowly; its edges catch soft paper-white highlights (#EDE8DF). A single fine thread spins off the
spindle and falls straight down the vertical centre line of the frame. At the very centre of the frame the thread
splits into three threads that fall side by side, close together, to the bottom edge, all inside the middle third
of the frame. As they fall, the left and right threads slowly lose their colour and fade to a dull grey, half
transparent. The middle thread stays a deep, warm vermilion red (#E2462F), like natural pigment dye on wool, matte,
not glowing. The left and right thirds of the frame stay empty and dark. Soft side light, deep shadows, fine film
grain, visible fibres in the threads, shallow depth of field. The last second holds still: three threads hanging,
two grey and one vermilion.
```

Optional clause for generation 2 (only if used; it adds a second motion to a 4 s clip, which raises the risk of
morphing blades and hands):
```
In the last second, the closed blades of a pair of dark iron shears enter from the right edge and cut the right
grey thread; its lower half falls away out of frame. The vermilion thread is untouched.
```

Negative:
```
text, letters, numbers, words, captions, subtitles, watermark, logo, signature, people, hands, fingers, faces,
neon, glow, bloom, lens flare, glossy, plastic, shiny metal, chrome, saturated red, bright red, pure red, pink,
magenta, orange, blue light, coloured light, gradient background, bokeh balls, sparks, particles, smoke, fast
camera moves, zoom, rotation of the frame, flicker, strobing, morphing, a fourth thread, tangled threads, cartoon,
illustration, 3D render look, CGI
```

## After generation (measure before cutting in)
- ffprobe: duration, fps, resolution. Generated takes can come back near-static: compare the first and the last
  frame (the split and the two grey threads must be visible at the end).
- Crop check: render the 864×1080 centre crop of the first, middle and last frames; the spindle, the split and all
  three threads must be inside it.
- Palette lock: sample the background, the brightest highlight and the red thread. Background near Ink
  `#121212` (grade down anything lighter than about `#1E1E1E` outside the threads); highlights no warmer or
  brighter than Paper `#EDE8DF`; the red thread graded to Vermilion `#E2462F` (HSL about 8° / 76% / 54%). A red
  that reads as pure or saturated digital red (hue under about 5° with saturation over about 90%, the family of
  `#FF3B30`) is graded down or the take is rejected. The two faded threads must carry no red at all.
- Any letter-like shapes or a logo in the frame: reject the take. The fallback (no third generation) is a code-made
  clip: a Paper hairline falling and splitting in three on Ink, two lines fading to 45% Paper, one Vermilion.
- Re-encode with the BT.709 tags the terminal footage gets, so the blacks match at the cut.
- Balance after; write the generation ids, model and credits spent into the video's credits note.

## Title overlay on the opener (beat B2)
Sizes below are this plan's working values; where the wordmark and card specs in `theme/` give others, those win.
- Wordmark **clotho.**: Space Grotesk 500, tracking about −4%, Paper, the dot in Vermilion; 160 px (16:9) /
  120 px (4:5); lower third, centred.
- Tagline under it: Outfit 400, Paper; 48 px / 44 px: **Spin every future. Keep one.**
- Fades in at 8.4 s over the clip's held last frame; holds to 10.4 s; cut to the terminal.

## Close overlay (beat B9, 71.8–79.0 s)
- Wordmark **clotho.** and the tagline, as in B2 (smaller: 112 px / 96 px).
- Install line, Dank Mono (it is a command), Ink text on a Vermilion fill (the install CTA; Ink on Vermilion is
  4.59:1): `hermes plugins install NicWeinbrecht/hermes-clotho --enable`, the README's install line (59
  characters: 44 px in 16:9 and 28 px in 4:5 both fit on one line). Without `--enable` the install ends "Plugin
  installed but not enabled".
- Repo, Outfit, Paper: `github.com/NicWeinbrecht/hermes-clotho`
- Credit, Outfit, Paper at 70%: `for the Hermes Agent · @NousResearch`
- Overlays fade out 0.4 s before the end; the last frame is the held red thread on Ink.
