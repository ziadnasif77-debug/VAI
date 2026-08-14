"""Effect planning and budget enforcement (SPEC §68, §69, §70).

Given the moments that survived selection and the resolved editing intent, this
produces the effects for one edit. It is deterministic: the same moments and the
same intent yield the same plan, which is what makes effect behaviour reviewable
and testable without a model in the loop.

The hard rule is §69: never apply an effect globally. Every effect is earned by
a specific moment, and three budgets stop them accumulating:

* per effect  -- its own ``max_per_minute`` / ``max_per_video`` / ``min_gap``
* per moment  -- ``max_effects_per_moment``, and one per category
* per video   -- ``max_effects_per_minute`` and a minimum gap between any two
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.config.schema import AppConfig
from backend.core.logging import LogChannel, get_logger
from backend.core.models.enums import (
    EffectCategory,
    EffectType,
    GameEventType,
    MomentType,
)
from backend.effects.library import EffectDefinition, EffectLibrary
from backend.effects.models import EffectCandidate, EffectInstance, EffectPlan
from backend.interaction.models import EditingIntent

logger = get_logger("effects.planner", LogChannel.RENDERING)

#: Where in a moment each category lands, as a fraction of the moment's length.
#: The offsets are spread rather than stacked so several effects on one moment
#: land far enough apart to survive the minimum-gap rule -- and so the viewer
#: sees the action before a graphic names it.
_CATEGORY_OFFSET: dict[EffectCategory, float] = {
    EffectCategory.TRANSITION: 0.00,
    EffectCategory.FRAME: 0.12,
    EffectCategory.CAMERA: 0.25,
    EffectCategory.TIME: 0.42,
    EffectCategory.LIGHT: 0.55,
    EffectCategory.AUDIO: 0.63,
    EffectCategory.GRAPHIC: 0.75,
    EffectCategory.TEXT: 0.88,
}


@dataclass(frozen=True, slots=True)
class PlannedMoment:
    """A moment as the planner sees it.

    Deliberately not the database row: the planner works from timeline
    positions, because an effect is placed in the finished video, not in the
    source recording.
    """

    id: str
    moment_type: MomentType
    timeline_start: float
    timeline_end: float
    score: float
    events: list[GameEventType] = field(default_factory=list)
    clip_id: str | None = None

    @property
    def duration_seconds(self) -> float:
        return self.timeline_end - self.timeline_start


@dataclass(slots=True)
class _Budget:
    """Running counters for one planning pass."""

    per_effect_counts: dict[EffectType, int] = field(default_factory=dict)
    last_effect_time: dict[EffectType, float] = field(default_factory=dict)
    placed: list[EffectInstance] = field(default_factory=list)

    def count(self, effect: EffectType) -> int:
        return self.per_effect_counts.get(effect, 0)

    def record(self, instance: EffectInstance) -> None:
        self.per_effect_counts[instance.effect] = self.count(instance.effect) + 1
        self.last_effect_time[instance.effect] = instance.start_seconds
        self.placed.append(instance)


class EffectPlanner:
    """Turns selected moments into a bounded set of effects."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._effects = config.effects
        self._library = EffectLibrary(config)

    def plan(
        self,
        moments: Sequence[PlannedMoment],
        intent: EditingIntent,
        *,
        video_duration_seconds: float,
    ) -> EffectPlan:
        """Produce the effects for one edit.

        Args:
            moments: selected moments, positioned on the finished timeline.
            intent: the resolved editing brief. Its ``effects`` dial and
                ``style`` decide how much decoration is appropriate.
            video_duration_seconds: length of the edit, used for the per-minute
                budget.
        """
        style = intent.style
        intensity = self._library.intensity_for(intent.effects, style)

        if not self._effects.enabled or intensity <= 0.0:
            reason = (
                "effects disabled in configuration"
                if not self._effects.enabled
                else f"intent requests {intent.effects.value} effects"
            )
            return EffectPlan(intensity=0.0, style=style, rejected=[reason])

        candidates = self._collect(
            moments, style, intensity, ranked=self._ranked_scores(moments)
        )
        instances, rejected = self._enforce_budgets(
            candidates, intensity=intensity, video_duration_seconds=video_duration_seconds
        )

        plan = EffectPlan(
            instances=sorted(instances, key=lambda item: item.start_seconds),
            rejected=rejected,
            intensity=intensity,
            style=style,
        )
        logger.info(
            "Effect plan built",
            extra={
                "style": style,
                "intensity": intensity,
                "candidates": len(candidates),
                "placed": plan.count,
                "rejected": len(rejected),
                "density_per_minute": round(
                    plan.density_per_minute(video_duration_seconds), 2
                ),
            },
        )
        return plan

    # -- candidate collection -------------------------------------------

    def _ranked_scores(self, moments: Sequence[PlannedMoment]) -> dict[str, float]:
        """Each moment's standing among this video's own moments, 0-1.

        A trigger's ``min_score`` reads as "how good, absolutely" -- and the
        library's numbers were calibrated on footage with a game profile, a
        microphone and detected reactions. Analyse a recording without those
        and the reaction, skill and narrative dimensions have no evidence to
        weigh, so the entire distribution shifts down: a real session's best
        moment scored 0.53 against a lowest threshold of 0.55, and the effects
        engine went silent on the exact footage §23 promises to support.

        So a moment is ranked against its peers. The best moment of any video
        ranks 1.0 and can earn what its type allows; the median ranks 0.5 and
        earns what a middling moment should. What ranking cannot do is rescue
        a video where nothing happened -- ``absolute_floor`` still applies to
        the raw score.
        """
        if not self._effects.relative_thresholds or not moments:
            return {moment.id: moment.score for moment in moments}

        scores = sorted(moment.score for moment in moments)
        lowest, highest = scores[0], scores[-1]
        if highest - lowest < 1e-9:
            # Every moment scored the same: ranking says nothing, so keep the
            # raw score rather than promoting them all to 1.0.
            return {moment.id: moment.score for moment in moments}

        ranked: dict[str, float] = {}
        for moment in moments:
            if moment.score < self._effects.absolute_floor:
                ranked[moment.id] = moment.score
                continue
            position = sum(1 for value in scores if value < moment.score)
            ranked[moment.id] = position / max(len(scores) - 1, 1)
        return ranked

    def _collect(
        self,
        moments: Sequence[PlannedMoment],
        style: str,
        intensity: float,
        *,
        ranked: dict[str, float] | None = None,
    ) -> list[EffectCandidate]:
        """Every effect that any moment could justify, ranked."""
        candidates: list[EffectCandidate] = []
        ranked = ranked or {}
        for moment in moments:
            definitions = self._library.candidates_for(
                moment_type=moment.moment_type,
                events=moment.events,
                score=ranked.get(moment.id, moment.score),
                style=style,
            )
            for definition in definitions:
                multiplier = self._library.priority_multiplier(definition.effect, style)
                if multiplier <= 0.0:
                    continue
                if not self._has_required_evidence(definition, moment):
                    continue
                start = self._placement(moment, definition)
                duration = self._duration(definition, moment, intensity, start)
                if duration <= 0.0:
                    continue
                candidates.append(
                    EffectCandidate(
                        effect=definition.effect,
                        moment_id=moment.id,
                        moment_type=moment.moment_type,
                        start_seconds=start,
                        duration_seconds=duration,
                        clip_id=moment.clip_id,
                        moment_score=moment.score,
                        priority=moment.score * multiplier,
                        matched_events=list(moment.events),
                        reason=self._reason(definition, moment),
                    )
                )
        # Strongest moments claim their effects first, so a budget spent early
        # is spent on the best material.
        return sorted(candidates, key=lambda item: (-item.priority, item.start_seconds))

    @staticmethod
    def _has_required_evidence(
        definition: EffectDefinition, moment: PlannedMoment
    ) -> bool:
        """Whether the detected data an effect declares it needs actually exists.

        ``require_detected_region`` means "drawn around something a detector
        found" -- and the renderer's fallback is a centred default box, a
        marker around nothing. ``require_event_count`` means "the event stream
        carries a tally worth showing": one countable event is not a streak,
        and the renderer's default would proudly display "x1". A moment type
        alone can match the trigger list, so the guard has to live here, not
        in the trigger match.
        """
        params = definition.spec.params
        if params.get("require_detected_region") and not params.get("region"):
            return False
        return not params.get("require_event_count") or _countable(definition, moment) >= 2

    def _placement(self, moment: PlannedMoment, definition: EffectDefinition) -> float:
        """Where in the moment an effect belongs.

        Camera and time effects land on the beat; graphics and text land after
        it, so the viewer sees what happened before being told about it. The
        per-category offsets also keep several effects on one moment far enough
        apart to survive the minimum-gap rule.
        """
        offset = _CATEGORY_OFFSET.get(definition.category, 0.45)
        placement = moment.timeline_start + moment.duration_seconds * offset
        return min(max(placement, moment.timeline_start), moment.timeline_end)

    def _duration(
        self,
        definition: EffectDefinition,
        moment: PlannedMoment,
        intensity: float,
        start_seconds: float,
    ) -> float:
        """Configured duration, scaled by intensity and clamped to the moment.

        Clamped against the time remaining from ``start_seconds``, not against
        the moment's whole length: an effect placed late in a moment must still
        finish inside it.
        """
        base = float(definition.spec.params.get("duration_seconds", 1.0) or 1.0)
        scaled = base * (0.7 + 0.3 * intensity)
        remaining = moment.timeline_end - start_seconds
        if remaining <= 0.0:
            return 0.0
        return max(min(scaled, remaining), 0.05)

    def _reason(self, definition: EffectDefinition, moment: PlannedMoment) -> str:
        events = ", ".join(event.value for event in moment.events[:3])
        detail = f" after {events}" if events else ""
        return (
            f"{definition.effect.value} on a {moment.moment_type.value} moment "
            f"scoring {moment.score:.2f}{detail}"
        )

    # -- budgets ---------------------------------------------------------

    def _enforce_budgets(
        self,
        candidates: Sequence[EffectCandidate],
        *,
        intensity: float,
        video_duration_seconds: float,
    ) -> tuple[list[EffectInstance], list[str]]:
        """Accept candidates until a budget says stop. §69's real enforcement."""
        limits = self._effects.global_limits
        minutes = max(video_duration_seconds / 60.0, 1 / 60.0)
        # Intensity scales the budget, not just the strength: a "minimal" edit
        # gets fewer effects as well as gentler ones.
        video_budget = int(limits.max_effects_per_minute * minutes * intensity)
        # ...and it scales how much decoration a single moment may collect, so
        # a sparse edit does not put three effects on every moment it has.
        moment_budget = max(1, round(limits.max_effects_per_moment * intensity))

        budget = _Budget()
        per_moment: dict[str, int] = {}
        per_moment_categories: dict[str, set[EffectCategory]] = {}
        rejected: list[str] = []

        for candidate in candidates:
            definition = self._library.definition(candidate.effect)
            verdict = self._check(
                candidate,
                definition,
                budget=budget,
                per_moment=per_moment,
                per_moment_categories=per_moment_categories,
                video_budget=video_budget,
                moment_budget=moment_budget,
                minutes=minutes,
                intensity=intensity,
            )
            if verdict is not None:
                rejected.append(
                    f"{candidate.effect.value} @ {candidate.start_seconds:.1f}s: {verdict}"
                )
                continue

            params = self._library.params_for(candidate.effect, intensity)
            if candidate.effect is EffectType.TEXT_POP and not params.get("text"):
                # The renderer draws nothing without text, and the library has
                # no way to know what happened. A detected event label -- or
                # failing that the moment's own type -- is detected metadata,
                # which keeps the config's "never invented" rule intact.
                params["text"] = _pop_label(candidate, definition, params)
            if params.get("require_event_count") and not params.get("count"):
                # The evidence guard admitted this candidate because a tally
                # exists; the renderer defaults an absent count to 1, so the
                # tally has to travel or the guard was for nothing.
                params["count"] = sum(
                    1
                    for event in candidate.matched_events
                    if event.value in definition.spec.triggers.events
                )
            instance = EffectInstance(
                effect=candidate.effect,
                engine=definition.engine,
                category=definition.category,
                start_seconds=candidate.start_seconds,
                duration_seconds=candidate.duration_seconds,
                clip_id=candidate.clip_id,
                moment_id=candidate.moment_id,
                params=params,
                strength=intensity,
                reason=candidate.reason,
            )
            budget.record(instance)
            per_moment[candidate.moment_id] = per_moment.get(candidate.moment_id, 0) + 1
            per_moment_categories.setdefault(candidate.moment_id, set()).add(
                definition.category
            )

        return budget.placed, rejected

    def _check(
        self,
        candidate: EffectCandidate,
        definition: EffectDefinition,
        *,
        budget: _Budget,
        per_moment: dict[str, int],
        per_moment_categories: dict[str, set[EffectCategory]],
        video_budget: int,
        moment_budget: int,
        minutes: float,
        intensity: float,
    ) -> str | None:
        """Return the reason to reject a candidate, or ``None`` to accept it."""
        limits = self._effects.global_limits
        own = definition.spec.limits

        if len(budget.placed) >= video_budget:
            return "video effect budget exhausted"
        if per_moment.get(candidate.moment_id, 0) >= moment_budget:
            return "moment already has its maximum effects"
        if definition.category in per_moment_categories.get(candidate.moment_id, set()):
            # Two camera moves on one moment read as one confused camera move.
            return f"moment already has a {definition.category.value} effect"

        if budget.count(candidate.effect) >= own.max_per_video:
            return "effect reached its per-video limit"
        if budget.count(candidate.effect) >= own.max_per_minute * minutes * intensity:
            return "effect reached its per-minute rate"

        last = budget.last_effect_time.get(candidate.effect)
        if last is not None and candidate.start_seconds - last < own.min_gap_seconds:
            return "too soon after the same effect"

        for placed in budget.placed:
            gap = abs(candidate.start_seconds - placed.start_seconds)
            if gap < limits.min_gap_seconds:
                return "too close to another effect"
            if (
                gap < limits.min_gap_seconds * 2
                and placed.category is definition.category
                and limits.max_consecutive_same_category <= 1
            ):
                return f"follows another {definition.category.value} effect too closely"

        return None


def _countable(definition: EffectDefinition, moment: PlannedMoment) -> int:
    """How many of the moment's events this effect's triggers can tally."""
    listened = set(definition.spec.triggers.events)
    return sum(1 for event in moment.events if event.value in listened)


def _pop_label(
    candidate: EffectCandidate, definition: EffectDefinition, params: dict[str, object]
) -> str:
    """The text a ``text_pop`` shows: a trigger-listed event, else the type.

    Only an event this effect's triggers actually listen for may name it --
    ``matched_events`` carries *all* of the moment's events, and on real
    footage that is dominated by ``unexpected_event``, which as an on-screen
    label would read as the software confessing.
    """
    listened = set(definition.spec.triggers.events)
    named = next(
        (event for event in candidate.matched_events if event.value in listened), None
    )
    label = named.value.replace("_", " ") if named else candidate.moment_type.value
    limit = params.get("max_characters")
    # bool is an int; YAML "max_characters: true" must not become a
    # one-character label.
    length = int(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else 24
    return label.upper()[:length]


__all__ = ["EffectPlanner", "PlannedMoment"]
