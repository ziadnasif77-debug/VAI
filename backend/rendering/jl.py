"""J-cuts and L-cuts: the audio leads or trails the picture at a boundary.

The hard A/V cut — picture and sound switching on the same frame — is the
join this pipeline has always made, and film grammar treats it as the marked
case, not the default: the standard join inside a scene lets the incoming
clip's sound arrive a beat before its picture (a **J-cut**) or the outgoing
clip's sound finish over the new picture (an **L-cut**). Researched for the
PLAN's open decision on 2026-08-14; what blocked it was mechanical, not
editorial. The render builds per-clip files and concatenates them, and a
concatenation cannot express two clips' audio at the same instant.

The contained rework, and where this module draws its line: the **video path
is untouched** — its §47 segment reuse survives byte-identical — and the
gameplay *audio* track is assembled as one placed-and-mixed FFmpeg graph
instead of a concat, then carried into the final composite exactly as the
concat's output was.

The semantics, exactly (§ the J case; L mirrors it): at the internal boundary
between clips A and B at timeline time ``t_cut``, a J-cut of ``dt`` seconds
means B's audio is heard from ``t_cut - dt`` while A's picture still shows,
and what is heard is B's *source* audio from ``[source_in - dt, source_in]``
— the sound that precedes B's first frame. At ``t_cut`` the audio arrives at
``source_in`` exactly, so B stays in sync for its whole body. That needs
material before ``source_in``, which is why ``dt`` is clamped to what the
recording has, and to half of each neighbouring clip so an offset never
swallows a short clip.

**The construction is adelay + amix with fade envelopes**, chosen over
``acrossfade`` deliberately: acrossfade's fade *is* its overlap, so a 0.6 s
lead would become a 0.6 s blend — incoming speech ramping up under a fade is
what a J-cut exists to avoid. Here each clip is trimmed with its lead and
trail, faded briefly at every seam (``crossfade_ms``, just enough to take
the waveform through zero), delayed to its timeline position, and summed by
``amix=normalize=0`` — the same mixer, with the same no-renormalising flag,
the final mix already uses.

QA note: the audio-sync check (§76) measures the *global* A/V offset of the
finished file, and this construction cannot move it — every clip's body
still maps ``[source_in, source_out]`` onto ``[timeline_start,
timeline_end]`` exactly as the concat did, and only material from outside
the body enters the overlap windows. Mid-clip sync is unchanged by
construction, not by measurement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from backend.config.schema import JLCutsConfig
from backend.rendering.audio_mix import MIX_FORMAT
from backend.timeline.models import Timeline, TimelineClip

BoundaryKind = Literal["j", "l", "hard"]

#: Below this an offset is a rounding error, not an edit: it is shorter than
#: the fade that would smooth it, so nothing of the lead would be heard at
#: full level. Such boundaries stay hard.
_MIN_OFFSET_SECONDS: Final[float] = 0.05

#: Fade length at a boundary that stays hard, mirroring the micro fade the
#: concat path applies (`render_worker._JOIN_FADE_SECONDS`): below anything a
#: listener registers as a fade, above anything that still clicks.
_HARD_EDGE_FADE_SECONDS: Final[float] = 0.03


@dataclass(frozen=True, slots=True)
class BoundaryPlan:
    """One internal boundary's verdict: which cut it is, and by how much.

    ``index`` counts boundaries, not clips: boundary ``i`` sits between clip
    ``i`` and clip ``i + 1`` of the enabled video clips in timeline order.
    There is no boundary before the first clip or after the last — the
    timeline's own start and end are never offset.
    """

    index: int
    kind: BoundaryKind
    dt: float = 0.0

    @property
    def is_hard(self) -> bool:
        return self.kind == "hard"


def plan_boundaries(
    timeline: Timeline,
    transcript_by_media: Mapping[str, Sequence[tuple[float, float]]],
    config: JLCutsConfig,
    *,
    source_durations: Mapping[str, float] | None = None,
) -> list[BoundaryPlan]:
    """Decide J, L or hard for every internal boundary. Pure; no I/O.

    The rule reads speech, because speech is what the offset protects: a line
    that begins with the incoming clip earns a J (the words hook the viewer
    before the picture changes), a line still running at the outgoing clip's
    end earns an L (the sentence finishes instead of being beheaded), and a
    boundary with neither stays hard — gunfire does not need a lead.

    Args:
        timeline: the edit; only its enabled video clips are read.
        transcript_by_media: speech spans per recording, as ``(start, end)``
            pairs in **source** seconds — the coordinates transcripts are
            stored in.
        config: the ``render.jl_cuts`` section.
        source_durations: each recording's probed length, when known. An
            L-cut reads past ``source_out``, and a recording's end is the one
            clamp planning cannot infer from the timeline; unknown means the
            trail is allowed and the extraction simply runs out of file.
    """
    clips = timeline.video_clips()
    plans: list[BoundaryPlan] = []
    for index in range(len(clips) - 1):
        outgoing, incoming = clips[index], clips[index + 1]
        plans.append(
            _plan_one(
                index,
                outgoing,
                incoming,
                transcript_by_media,
                config,
                source_durations or {},
            )
        )
    return plans


def offsets(
    clips: Sequence[TimelineClip], boundaries: Sequence[BoundaryPlan]
) -> list[tuple[float, float]]:
    """Each clip's ``(lead, trail)`` in seconds, from the boundary verdicts.

    A clip's lead comes from the boundary *before* it being a J; its trail
    from the boundary *after* it being an L. The first clip can therefore
    never lead and the last can never trail, which is the "never the
    timeline's first start or last end" rule falling out of the shape rather
    than being policed.
    """
    if len(boundaries) != max(len(clips) - 1, 0):
        raise ValueError(
            f"{len(clips)} clip(s) have {len(clips) - 1} internal boundaries; "
            f"{len(boundaries)} plan(s) were given"
        )
    placed: list[tuple[float, float]] = []
    for position in range(len(clips)):
        before = boundaries[position - 1] if position > 0 else None
        after = boundaries[position] if position < len(boundaries) else None
        lead = before.dt if before is not None and before.kind == "j" else 0.0
        trail = after.dt if after is not None and after.kind == "l" else 0.0
        placed.append((lead, trail))
    return placed


def assembly_arguments(
    clips: Sequence[TimelineClip],
    boundaries: Sequence[BoundaryPlan],
    *,
    sources: Mapping[str, Path],
    destination: Path,
    config: JLCutsConfig,
) -> list[str]:
    """The argv that assembles the offset gameplay track, returned to be run.

    Built as an inspectable list and executed by the caller's runner, the way
    the whole rendering layer does it. One invocation, one graph: every clip
    is trimmed with its lead and trail, faded at each seam, delayed to its
    timeline position and summed — so the output is the full track the plain
    concat used to produce, with the boundaries overlapped.

    The output is written as PCM, like the concat's, because this file is an
    intermediate the final mix re-reads: a lossy pass here would be paid
    twice.
    """
    placed = offsets(clips, boundaries)
    crossfade = max(config.crossfade_ms, 0) / 1000.0

    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    for position, clip in enumerate(clips):
        lead, trail = placed[position]
        inputs += ["-i", str(sources[clip.media_id])]
        chains.append(
            _clip_chain(
                position,
                clip,
                lead=lead,
                trail=trail,
                fade_in=_seam_fade(boundaries, position - 1, crossfade),
                fade_out=_seam_fade(boundaries, position, crossfade),
            )
        )
        labels.append(f"[c{position}]")

    graph = ";".join(
        [
            *chains,
            # normalize=0 for the same reason the final mix says it: amix
            # divides by the input count by default, and a seventy-clip edit
            # summed at a seventieth of its level is not a quieter mix, it is
            # a silent one.
            f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[jl]",
        ]
    )
    return [
        *inputs,
        "-filter_complex", graph,
        "-map", "[jl]",
        "-c:a", "pcm_s16le",
        str(destination),
    ]


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _plan_one(
    index: int,
    outgoing: TimelineClip,
    incoming: TimelineClip,
    transcript_by_media: Mapping[str, Sequence[tuple[float, float]]],
    config: JLCutsConfig,
    source_durations: Mapping[str, float],
) -> BoundaryPlan:
    """One boundary's verdict."""
    hard = BoundaryPlan(index=index, kind="hard")
    if not config.enabled:
        return hard
    if outgoing.speed != 1.0 or incoming.speed != 1.0:
        # A speed-warped clip's timeline seconds are not its source seconds,
        # and an offset computed in one and extracted in the other would
        # desync the very boundary it decorates.
        return hard

    window = config.speech_window_seconds
    #: Never more than half of either neighbour: an offset that eats half a
    #: clip has replaced the cut, not softened it.
    room = min(
        config.max_lead_seconds, outgoing.duration / 2.0, incoming.duration / 2.0
    )

    if _speech_overlaps(
        transcript_by_media.get(incoming.media_id, ()),
        incoming.source_in,
        incoming.source_in + window,
    ):
        # The incoming clip opens on speech: hear it before seeing it. The
        # lead plays source material from before the in-point, so there must
        # be that much recording to play.
        dt = min(room, incoming.source_in)
        if dt >= _MIN_OFFSET_SECONDS:
            return BoundaryPlan(index=index, kind="j", dt=dt)
        return hard

    if _speech_overlaps(
        transcript_by_media.get(outgoing.media_id, ()),
        outgoing.source_out - window,
        outgoing.source_out,
    ):
        # The outgoing clip ends mid-speech: let the sentence finish. The
        # trail reads past the out-point, clamped to the recording's end when
        # that end is known; unknown, the extraction clamps by running out.
        dt = room
        known = source_durations.get(outgoing.media_id)
        if known is not None:
            dt = min(dt, max(known - outgoing.source_out, 0.0))
        if dt >= _MIN_OFFSET_SECONDS:
            return BoundaryPlan(index=index, kind="l", dt=dt)
        return hard

    return hard


def _speech_overlaps(
    spans: Sequence[tuple[float, float]], lo: float, hi: float
) -> bool:
    """Whether any transcript span crosses ``[lo, hi]`` in source seconds."""
    return any(start < hi and end > lo for start, end in spans if end > start)


def _seam_fade(
    boundaries: Sequence[BoundaryPlan], index: int, crossfade: float
) -> float:
    """The fade a clip edge needs at boundary ``index``.

    An offset boundary gets the configured crossfade so neither stream starts
    or stops mid-waveform against the other; a hard boundary — and the
    timeline's outer edges, where ``index`` falls off either end — keeps the
    micro fade the concat path has always applied.
    """
    if 0 <= index < len(boundaries) and not boundaries[index].is_hard:
        return crossfade
    return _HARD_EDGE_FADE_SECONDS


def _clip_chain(
    position: int,
    clip: TimelineClip,
    *,
    lead: float,
    trail: float,
    fade_in: float,
    fade_out: float,
) -> str:
    """One clip's filter chain: trim with offsets, fade the seams, place.

    The trim reads ``[source_in - lead, source_out + trail]`` and the delay
    places it at ``timeline_start - lead``, which is what keeps the body's
    mapping identical to the concat's: the lead shifts the start of the
    *extract* and the start of the *placement* by the same amount, so every
    source second inside the body still lands on its own timeline second.
    """
    start = clip.source_in - lead
    end = clip.source_out + trail
    duration = end - start
    fade_in = min(fade_in, duration / 4)
    fade_out = min(fade_out, duration / 4)
    chain = (
        f"[{position}:a:0]atrim=start={start:.6f}:end={end:.6f},"
        f"asetpts=N/SR/TB,{MIX_FORMAT}"
        f",afade=t=in:st=0:d={fade_in:.3f}"
        f",afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}"
    )
    delay_ms = round(max(0.0, clip.timeline_start - lead) * 1000)
    if delay_ms:
        chain += f",adelay={delay_ms}|{delay_ms}"
    return chain + f"[c{position}]"


__all__ = [
    "BoundaryPlan",
    "assembly_arguments",
    "offsets",
    "plan_boundaries",
]
