"""Content QA (SPEC §77, §78).

The technical checks ask whether the file is broken. These ask whether the
*edit* is bad, which is a different question with a different answer type: §78
says the system must never assume its own decisions are correct, so nothing
here fails a render. Everything is a warning addressed to a person.

**Nothing here decodes the video again.** The pipeline already looked at this
footage — vision described the candidate frames, the audio pass found the
silences, the narrative stage measured its own pacing. Re-watching twenty
minutes to rediscover that would cost more than the render and find less, since
a second pass would have none of the context the first one built.

So each check reads stored analysis and asks a question about the *finished
timeline*: not "is there a menu in this recording" — there usually is — but
"did a menu screen end up in the edit". That is the difference between a
detector and a QA check.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from backend.config.schema import CaptionsConfig, QaConfig
from backend.core.logging import LogChannel, get_logger
from backend.gaming.profiles import GameProfile, Region
from backend.qa.report import Finding, enabled_checks, passed, warning
from backend.timeline.captions import Caption
from backend.timeline.models import Timeline, TimelineClip

logger = get_logger("qa.content", LogChannel.QA)

CHECKS: Final[tuple[str, ...]] = (
    "unexpected_blank_screen",
    "accidental_menu_section",
    "broken_sequence",
    "extreme_silence",
    "bad_transition",
    "caption_covers_hud",
)

#: Vision labels that mean "this is not gameplay". Matched case-insensitively
#: against whole labels, so "menu" does not fire on "menuever".
MENU_LABELS: Final[frozenset[str]] = frozenset(
    {"menu", "loading", "lobby", "settings", "pause", "scoreboard", "inventory"}
)

#: A clip shorter than this reads as a flash rather than a cut (§77's "bad
#: transition"). Not a hard rule -- a quick reaction cut is legitimate -- which
#: is why it warns.
MIN_COMFORTABLE_CLIP_SECONDS: Final[float] = 1.2


@dataclass(frozen=True, slots=True)
class ContentInputs:
    """What the checks read. All of it already exists by the time QA runs.

    Timestamps are in **source** time, because that is how the analysis stored
    them; each check maps them onto the timeline itself, which is the mapping
    that makes "did it end up in the edit" answerable.
    """

    #: ``(media_id, timestamp, labels)`` from the vision pass.
    observations: Sequence[tuple[str, float, Sequence[str]]] = ()
    #: ``(media_id, start, end)`` silences from the audio pass.
    silences: Sequence[tuple[str, float, float]] = ()
    #: Warnings the narrative stage already produced about its own pacing.
    pacing_warnings: Sequence[str] = ()
    #: Black runs technical QA measured, in timeline time.
    black_runs: Sequence[tuple[float, float]] = ()
    profile: GameProfile | None = None


def inspect(
    timeline: Timeline,
    captions: Sequence[Caption],
    inputs: ContentInputs,
    *,
    config: QaConfig,
    caption_config: CaptionsConfig,
) -> list[Finding]:
    """Run every enabled content check against the finished edit (§77)."""
    if not config.content.enabled:
        return []

    checks = set(enabled_checks(CHECKS, config.content.checks))
    findings: list[Finding] = []

    if "accidental_menu_section" in checks:
        findings.append(_menu_sections(timeline, inputs))
    if "extreme_silence" in checks:
        findings.append(_extreme_silence(timeline, inputs, config))
    if "unexpected_blank_screen" in checks:
        findings.append(_blank_screens(inputs))
    if "broken_sequence" in checks:
        findings.append(_broken_sequence(inputs))
    if "bad_transition" in checks:
        findings.append(_bad_transitions(timeline))
    if "caption_covers_hud" in checks:
        findings.append(_captions_over_hud(captions, caption_config, inputs.profile))

    logger.info(
        "Content QA complete",
        extra={
            "checks": len(findings),
            "warnings": sum(1 for item in findings if item.warned),
        },
    )
    return findings


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _menu_sections(timeline: Timeline, inputs: ContentInputs) -> Finding:
    """Did a menu or loading screen survive into the edit? (§77)

    Menus are not a defect in a recording -- every session has them. They are a
    defect in a *video*, and the difference is whether a clip covers one.
    """
    # The same evidence floor the screen guard cuts by: one sampled frame
    # is a claim, not a section. Menu observations convict in RUNS -- at
    # least two of them within a sampling stride of each other -- because a
    # single misread (a phone overlay, a busy HUD) flagged seventeen
    # "sections" on a real edit while the guard rightly refused to cut any
    # of them. Consistency: weak evidence counts nowhere, or everywhere.
    menu_hits: list[tuple[str, float, list[str]]] = []
    for media_id, timestamp, labels in inputs.observations:
        if _is_menu(labels):
            menu_hits.append(
                (media_id, timestamp, [label for label in labels if _is_menu([label])])
            )
    menu_hits.sort(key=lambda hit: (hit[0], hit[1]))

    offenders: list[dict[str, Any]] = []
    run: list[tuple[str, float, list[str]]] = []
    for hit in [*menu_hits, ("", -1.0, [])]:
        if run and (hit[0] != run[-1][0] or hit[1] - run[-1][1] > 20.0):
            if len(run) >= 2:
                for media_id, timestamp, labels in run:
                    clip = _clip_covering(timeline, media_id, timestamp)
                    if clip is not None:
                        offenders.append(
                            {
                                "clip_index": clip.clip_index,
                                "timeline_seconds": round(
                                    clip.timeline_start + (timestamp - clip.source_in),
                                    2,
                                ),
                                "labels": labels,
                            }
                        )
            run = []
        if hit[0]:
            run.append(hit)

    if not offenders:
        return passed("accidental_menu_section", "no menu or loading screen in the edit")
    where = ", ".join(f"{item['timeline_seconds']:.0f}s" for item in offenders[:4])
    return warning(
        "accidental_menu_section",
        f"{len(offenders)} moment(s) in the edit show a menu or loading screen ({where})",
        remedy="Watch those timestamps and remove the clips if they are not intentional.",
        occurrences=offenders[:10],
    )


def _extreme_silence(
    timeline: Timeline, inputs: ContentInputs, config: QaConfig
) -> Finding:
    """A long stretch of nothing happening, inside the finished video (§77)."""
    limit = config.content.thresholds.max_silence_seconds
    longest = 0.0
    where = 0.0
    for media_id, start, end in inputs.silences:
        clip = _clip_covering(timeline, media_id, start)
        if clip is None:
            continue
        # Only the part of the silence the edit actually kept counts.
        visible_end = min(end, clip.source_out)
        length = visible_end - start
        if length > longest:
            longest = length
            where = clip.timeline_start + (start - clip.source_in)

    if longest <= limit:
        return passed("extreme_silence", f"longest silence in the edit {longest:.1f}s")
    return warning(
        "extreme_silence",
        f"{longest:.1f}s of silence at {where / 60:.1f} min, longer than the "
        f"{limit:.0f}s the configuration allows",
        remedy="Trim that clip, or lower moments.dead_time so the selection avoids it.",
        seconds=round(longest, 2),
        timeline_seconds=round(where, 2),
    )


def _blank_screens(inputs: ContentInputs) -> Finding:
    """Black stretches too short to fail the technical check but long to watch.

    The technical threshold is about a broken render; this is about a viewer
    noticing. A second of black between clips passes one and fails the other.
    """
    if not inputs.black_runs:
        return passed("unexpected_blank_screen", "no blank screens")
    longest = max(end - start for start, end in inputs.black_runs)
    return warning(
        "unexpected_blank_screen",
        f"{len(inputs.black_runs)} blank stretch(es), the longest {longest:.1f}s",
        remedy="Check the timeline for a gap or a clip that starts on a fade.",
        runs=len(inputs.black_runs),
        longest_seconds=round(longest, 2),
    )


def _broken_sequence(inputs: ContentInputs) -> Finding:
    """Reuse the narrative stage's own judgement about its pacing (§38, §77).

    It already measured the thing this check would have to re-derive: runs of
    one moment type, a flat intensity curve, a clip that outstays its welcome.
    Recomputing it here would be a second opinion from less information.
    """
    if not inputs.pacing_warnings:
        return passed("broken_sequence", "the pacing report found nothing")
    return warning(
        "broken_sequence",
        "; ".join(inputs.pacing_warnings[:3]),
        remedy="Reorder or drop clips, or change the video mode and re-run the story stage.",
        warnings=list(inputs.pacing_warnings),
    )


def _bad_transitions(timeline: Timeline) -> Finding:
    """Clips too short to register before the next one arrives (§77)."""
    flashes = [
        clip
        for clip in timeline.video_clips()
        if clip.duration < MIN_COMFORTABLE_CLIP_SECONDS
    ]
    if not flashes:
        return passed("bad_transition", "every clip is long enough to read")
    return warning(
        "bad_transition",
        f"{len(flashes)} clip(s) are under {MIN_COMFORTABLE_CLIP_SECONDS}s and will "
        "read as a flash",
        remedy="Lengthen or remove them; a cut this quick is rarely deliberate.",
        clips=[clip.clip_index for clip in flashes[:10]],
    )


def _captions_over_hud(
    captions: Sequence[Caption], config: CaptionsConfig, profile: GameProfile | None
) -> Finding:
    """Would a caption sit on top of something the viewer needs to read? (§77)

    Geometry, not vision: the caption's band is known from the layout config and
    the HUD's regions are declared by the profile, so this is a rectangle
    intersection rather than another look at the footage.
    """
    if not captions:
        return passed("caption_covers_hud", "no captions to place")
    if profile is None or not profile.regions:
        # The generic profile declares no regions (§23), so there is nothing to
        # collide with. Saying "checked, nothing known" beats silence.
        return passed(
            "caption_covers_hud", "no HUD regions are declared for this game"
        )
    if not config.layout.avoid_hud_regions:
        return passed("caption_covers_hud", "HUD avoidance is switched off")

    band = _caption_band(config)
    clashing = [
        name for name, region in profile.regions.items() if _overlaps(band, region)
    ]
    if not clashing:
        return passed("caption_covers_hud", "captions clear every declared HUD region")
    return warning(
        "caption_covers_hud",
        f"the caption band overlaps {', '.join(sorted(clashing))}",
        remedy=(
            "Raise captions.layout.safe_area_bottom, or move the caption position, "
            "so the HUD stays readable."
        ),
        regions=sorted(clashing),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_menu(labels: Iterable[str]) -> bool:
    return any(label.strip().lower() in MENU_LABELS for label in labels)


def _clip_covering(
    timeline: Timeline, media_id: str, source_seconds: float
) -> TimelineClip | None:
    """The enabled clip whose source span contains a source timestamp.

    The mapping the whole module depends on: analysis timestamps are in the
    recording, and a QA finding is only interesting if the edit kept that part.
    """
    for clip in timeline.video_clips():
        if clip.media_id != media_id:
            continue
        if clip.source_in <= source_seconds <= clip.source_out:
            return clip
    return None


def _caption_band(config: CaptionsConfig) -> Region:
    """Where captions are drawn, as a fraction of the frame.

    Approximated from the layout: the exact glyph box depends on the text, and
    a check that needed the text would have to render it.
    """
    height = min(0.28, config.appearance.font_size_ratio * config.layout.max_lines * 2.2)
    horizontal = config.layout.safe_area_horizontal
    width = max(0.05, 1.0 - horizontal * 2)
    if config.layout.position == "top_center":
        top = config.layout.safe_area_bottom
    elif config.layout.position == "center":
        top = max(0.0, 0.5 - height / 2)
    else:
        top = max(0.0, 1.0 - config.layout.safe_area_bottom - height)
    return Region(x=horizontal, y=top, width=width, height=min(height, 1.0 - top))


def _overlaps(left: Region, right: Region) -> bool:
    return (
        left.x < right.x + right.width
        and right.x < left.x + left.width
        and left.y < right.y + right.height
        and right.y < left.y + left.height
    )


__all__ = [
    "CHECKS",
    "MENU_LABELS",
    "MIN_COMFORTABLE_CLIP_SECONDS",
    "ContentInputs",
    "inspect",
]
