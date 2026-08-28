"""The doctrine's final output (docs/DIRECTION.md §27, §34), as two views.

Nothing here computes: every number was produced by a stage and stored, and
these endpoints only assemble. ``/edit-plan`` is §27's machine-readable
contract over the EDL that has always been the real edit plan; ``/report``
is §34's fifteen-item closing statement, quality score and uncertainty list
included, with "UNCERTAIN" carried exactly where the pipeline said it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.api.dependencies import AppState, get_state
from backend.core.models.enums import JobStage, TrackKind
from backend.database.repositories.jobs import JobRepository
from backend.database.repositories.media import MediaRepository
from backend.database.repositories.moments import MomentRepository
from backend.database.repositories.timeline import TimelineRepository
from backend.moments.scoring import tier_for

router = APIRouter(tags=["report"])


@router.get("/projects/{project_id}/edit-plan")
def edit_plan(project_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    """§27: the edit, machine-readable, from what the stages stored."""
    project = state.projects.get(project_id)
    return _edit_plan(state, project)


@router.get("/projects/{project_id}/report")
def final_report(project_id: str, state: AppState = Depends(get_state)) -> dict[str, Any]:
    """§34: the fifteen-item closing statement over the stored results."""
    project = state.projects.get(project_id)
    database = state.database
    jobs = JobRepository(database)

    moments: list[Any] = []
    for item in MediaRepository(database).list_for_project(project_id):
        moments.extend(MomentRepository(database).list_for_media(item.id))
    ranked = sorted(moments, key=lambda m: -float(getattr(m, "score", 0.0)))

    story = _result(jobs, project_id, JobStage.STORY)
    edl = _result(jobs, project_id, JobStage.EDL)
    render = _result(jobs, project_id, JobStage.RENDER)
    qa = _result(jobs, project_id, JobStage.QA)
    publish = _result(jobs, project_id, JobStage.PUBLISH)

    return {
        "analysis": {
            "moments": len(moments),
            "tiers": _tier_counts(ranked),
            "recordings": len(MediaRepository(database).list_for_project(project_id)),
        },
        "highlight_ranking": [
            {
                "type": str(getattr(m.moment_type, "value", m.moment_type)),
                "start_seconds": round(float(m.start_seconds), 2),
                "score": round(float(m.score) * 100),
                "tier": tier_for(float(m.score)),
            }
            for m in ranked[:10]
        ],
        "narrative_structure": (story.get("clips") and [
            {"beat": clip.get("beat"), "role": clip.get("role")}
            for clip in story["clips"]
        ]) or [],
        "hook": story.get("hook") or {"hook": None},
        "cut_plan": {
            "clips": edl.get("clips"),
            "duration_seconds": edl.get("duration_seconds"),
            "warnings": edl.get("warnings") or [],
        },
        "effect_plan": edl.get("effects"),
        "audio_plan": {"notes": [n for n in (render.get("notes") or []) if "audio" in str(n).lower() or "music" in str(n).lower() or "microphone" in str(n).lower()]},
        "text_plan": {"captions": edl.get("captions")},
        "thumbnail_plan": {"path": _thumbnail_path(state, project_id)},
        "youtube": (publish.get("request") or {}).get("metadata")
        or {"note": "UNCERTAIN: nothing has been published yet"},
        "edit_plan": _edit_plan(state, project),
        "quality_score": qa.get("quality_score"),
        "uncertainties": qa.get("uncertainties") or [],
    }


def _edit_plan(state: AppState, project) -> dict[str, Any]:
    database = state.database
    jobs = JobRepository(database)
    story = _result(jobs, project.id, JobStage.STORY)
    repository = TimelineRepository(database)
    clips = repository.list_clips(project.id, track=TrackKind.VIDEO)
    effects = repository.list_effects(project.id)

    by_clip: dict[str | None, list[dict[str, Any]]] = {}
    for effect in effects:
        by_clip.setdefault(getattr(effect, "clip_id", None), []).append(
            {
                "name": str(getattr(effect.effect, "value", effect.effect)),
                "start": round(float(getattr(effect, "start_seconds", 0.0)), 2),
                "duration": round(float(getattr(effect, "duration_seconds", 0.0)), 2),
                "intensity": getattr(effect, "intensity", None),
            }
        )

    roles: dict[int, dict[str, Any]] = {}
    for index, clip in enumerate(story.get("clips") or []):
        roles[index] = clip

    segments = []
    for index, clip in enumerate(clips):
        story_clip = roles.get(index, {})
        score = float(story_clip.get("score", 0.0) or 0.0)
        segments.append(
            {
                "id": f"segment_{index + 1:03d}",
                "source_start": round(clip.source_in, 2),
                "source_end": round(clip.source_out, 2),
                "type": story_clip.get("role") or story_clip.get("beat") or "body",
                "importance": round(score * 100),
                "tier": tier_for(score),
                "effects": by_clip.get(clip.id, []),
            }
        )

    hook = story.get("hook") or {}
    return {
        "project": {
            "type": "gaming_highlight",
            "target_platform": "youtube",
            "target_duration": project.target_duration_seconds,
        },
        "hook": hook,
        "segments": segments,
        "thumbnail": {"path": _thumbnail_path(state, project.id)},
    }


def _result(jobs: JobRepository, project_id: str, stage: JobStage) -> dict[str, Any]:
    job = jobs.find(project_id, stage, None)
    return dict(job.result) if job is not None and job.result else {}


def _tier_counts(ranked) -> dict[str, int]:
    counts: dict[str, int] = {}
    for moment in ranked:
        tier = tier_for(float(getattr(moment, "score", 0.0)))
        counts[tier] = counts.get(tier, 0) + 1
    return counts


def _thumbnail_path(state: AppState, project_id: str) -> str | None:
    path = state.paths.project(project_id).assets / "thumbnail.jpg"
    return str(path) if path.is_file() else None


__all__ = ["router"]
