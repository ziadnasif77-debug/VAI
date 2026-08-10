# Phase 9 — Remotion overlay

SPEC §64, §66, §67; decision D-008; §126 step 15. **Acceptance: EDL → Remotion
→ an alpha overlay that composites correctly.**

Status: **complete and verified.** `ruff` and `tsc` are clean, and the
acceptance is a measurement rather than an assertion — compositing the overlay
over known footage changes **zero pixels** before the caption appears.

---

## Delivered

| Requirement | Where | Verified by |
| --- | --- | --- |
| Composition description (§64) | `backend/rendering/composition.py` | `test_composition.py` — 24 tests |
| Overlay project (§66) | `remotion/src/` | `tsc --noEmit`, and the render itself |
| Captions (§71) | `remotion/src/Caption.tsx` | `TestAcceptance` |
| Overlay effects (§68) | `remotion/src/Effects.tsx` | `TestOnlyOverlayWork` |
| Invocation and skip | `backend/rendering/remotion.py` | `test_remotion.py` — 15 tests |

---

## The acceptance is a measurement

"An alpha overlay that composites correctly" cannot be checked by looking at
the file. An overlay whose alpha channel is broken is an opaque rectangle over
the gameplay: it renders, it encodes, it uploads, and it is discovered by
watching the video.

So the test composites the overlay over generated footage and counts the pixels
that changed:

| | changed pixels |
| --- | --- |
| before the caption appears | **0** |
| during the caption | 16 430 of 230 400 (7 %) |

Zero is the number that matters. Anything else means the transparency is not
transparent.

---

## The trap that cost the most: VP9 alpha is invisible to `ffprobe`

`ffprobe` reports the finished overlay as `pix_fmt=yuv420p`. It looks like the
alpha channel was never written.

It was. VP9 in WebM carries alpha as a **per-block side channel** rather than in
the pixel format, and FFmpeg's *native* VP9 decoder discards it without a
word — so the overlay composites as an opaque rectangle and nothing anywhere
reports a problem. Naming `libvpx-vp9` on the input is the entire fix.

`overlay_input_arguments()` exists so no caller has to know that, and
`test_the_native_decoder_loses_the_alpha` asserts the failure still exists, so
the fix is not later mistaken for superstition and removed.

Rendering it needs three flags together, and two of them are easy to omit:
`--codec=vp9 --image-format=png --pixel-format=yuva420p`. PNG is not optional —
Remotion rasterises to JPEG by default, and JPEG has no alpha to carry.

---

## Two clocks, again

`useCurrentFrame()` inside a Remotion `<Sequence>` is **relative** to that
sequence: it returns 0 on the element's first frame. The word timings in the
composition are in **programme** frames, because that is where the transcript
put them.

The first implementation subtracted the sequence offset from a frame number
that had already had it subtracted, so captions were invisible for most of
their duration — and visible for a window in the middle, which is exactly the
kind of half-working that a quick look confirms as fine. Two acceptance tests
caught it; the conversion now happens once, at the top of the component, with a
comment saying which clock is which.

This is the same discipline the timeline applies to source time versus
programme time. Both places, the same lesson.

---

## Other decisions worth knowing

### The description *is* the props object

Remotion merges `defaultProps` with what `--props` supplies. A nested
`{composition: {...}}` shape looked tidier and failed silently: a mismatched key
left the defaults in place and rendered an empty 1080p canvas with no error at
all. The composition file is now exactly the props, so a key that does not
match is a type error rather than a blank video.

### Frames are computed in Python, not TypeScript

A caption at 1.234 s of a 60 fps video is frame 74.04. Rounding it in the
browser would put the decision somewhere untestable and let two elements
disagree about which frame they share. `seconds_to_frames` rounds to nearest —
right for a position — and `ceil_frames` rounds up, which is right for a
duration that must not come up short.

### `node <cli>.js`, not `npx`

`npx` is a shell shim, which on Windows means `npx.cmd` and a subprocess call
that cannot find it without a shell — and §85 says no shell. The installed CLI
is a plain JavaScript file, and `node` runs it identically on every platform.

### The pass is genuinely skippable

D-008's point. `Composition.is_empty` is true when there are no captions and no
Remotion-engine effects — an FFmpeg-only effect plan leaves it empty, because a
zoom is not drawn on this layer. A video with nothing to draw never starts
Chromium.

Drawn spans are computed too: a twenty-minute video with four minutes of
captions should cost four minutes of rendering, and Phase 10 has the spans it
needs to do that.

### A missing renderer degrades

§95. No Node, no `npm install`, `enabled: false` — each returns a skipped
result with a reason, and the caller produces a video without captions rather
than failing a render FFmpeg could still complete.

---

## Licence

Remotion is free for individuals and for-profit organisations with **up to
three employees**; above that it needs a company licence. Read from the licence
text itself, since the licensing docs page does not state the threshold. This
project is within the free tier.

The dependency stays optional by construction: the timeline never names an
engine (§67), and the overlay pass is skippable. Replacing Remotion would mean
writing a different overlay renderer, not touching the pipeline.

---

## Bugs found while building this

| Defect | Consequence had it shipped |
| --- | --- |
| The sequence offset was subtracted twice | Captions invisible for most of their duration, and visible in the middle — the kind of half-working a quick check confirms as fine |
| Props nested under a key Remotion does not merge into | An empty 1080p canvas, rendered successfully, with no error |
| `--pixel-format=yuva420p` omitted | No alpha channel at all |
| `npx` invoked as a subprocess | `FileNotFoundError` on Windows, where it is `npx.cmd` |
| Remotion adds a silent Opus track by default | An audio stream on a picture, which the composite would have to know to ignore |

---

## Not built, and why

| Deferred | Phase |
| --- | --- |
| Compositing the overlay onto gameplay | Phase 10. `overlay_input_arguments()` carries the decoder requirement across the boundary |
| Rendering only the drawn spans | Phase 10, which owns the render loop. The spans are computed and stored here |
| Intro/outro cards | §66 lists them; nothing in the pipeline produces one yet, and inventing content is §37's prohibition in a different costume |
| Fonts beyond the system stack | `@remotion/google-fonts` would fetch at render time. Local-first (§50) means bundling the file instead, and no font has been chosen |

---

## Gate to Phase 10

Met: `ruff` clean, `tsc` clean, and an overlay that measurably composites.

Phase 10 begins at §126 step 16: the final render (§65, §72–§75). Its shape is
already fixed by §67 — FFmpeg cuts and concatenates, Remotion's overlay is
composited on top, and the audio mix follows §72's priority: **speech above
important game audio above music**, with §74's ducking.
