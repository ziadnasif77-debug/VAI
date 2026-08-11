"""What the person did to the edit (SPEC §119).

    AI-selected moments accepted · AI-selected moments deleted · AI-rejected
    moments restored · average manual edits.

§118 scores the system against an opinion written down once. This scores it
against the opinion someone acts on, every time they use it — and that is the
better signal for exactly the reason it is harder to collect: nobody labels a
golden dataset for fun, and everybody deletes a clip they did not want.

Nothing new is recorded to compute this. The interaction layer already keeps
what it needs, because §42 asked for non-destructive editing and §78 asked for
the human to have the last word:

* the timeline's ``enabled`` flag, which is how "delete clip 5" is stored
* ``moments.user_state``, which is how a moment is rejected or restored
* ``edit_versions``, one per command, which is how many edits were made

So this module reads. It has no writer, no migration and no new table, and it
can be pointed at any project that has ever been edited — including projects
finished months ago.

**The numbers are only meaningful once someone has actually edited.** A project
where nothing was touched has 100% acceptance, which says nothing about
quality; :attr:`UserEditMetrics.edited` is carried so a caller can tell the
difference between agreement and absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.core.logging import LogChannel, get_logger
from backend.database.connection import Database

logger = get_logger("quality.user_edits", LogChannel.PIPELINE)

#: ``moments.user_state`` values. "auto" means the person never touched it.
AUTO = "auto"
ACCEPTED = "accepted"
REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class UserEditMetrics:
    """§119, for one project."""

    project_id: str

    #: Clips the pipeline chose and the person kept.
    selected_kept: int = 0
    #: Clips the pipeline chose and the person removed. The headline number:
    #: every one is a moment the scoring thought was worth 10-60 minutes of
    #: someone's attention and was not.
    selected_deleted: int = 0
    #: Moments the pipeline scored below the cut that the person put back.
    #: Rarer and more informative -- a deletion can be taste, but a restoration
    #: means the ranking was wrong about something it could see.
    rejected_restored: int = 0
    #: Moments explicitly marked good by the person, whether or not they made
    #: the cut.
    marked_accepted: int = 0

    #: Edit commands issued, from the version history.
    manual_edits: int = 0
    #: Clips in the edit, for the per-clip rate.
    clips: int = 0
    #: Whether this project was ever edited at all. Without it, an untouched
    #: project reports perfect agreement.
    edited: bool = False

    @property
    def acceptance_rate(self) -> float:
        """Of what the pipeline chose, how much survived (§119)."""
        total = self.selected_kept + self.selected_deleted
        return self.selected_kept / total if total else 1.0

    @property
    def deletion_rate(self) -> float:
        total = self.selected_kept + self.selected_deleted
        return self.selected_deleted / total if total else 0.0

    @property
    def edits_per_clip(self) -> float:
        """§119's "average manual edits", normalised by the size of the edit.

        Per clip rather than per project: ten edits on a 40-clip video and ten
        on a 5-clip video are not the same experience.
        """
        return self.manual_edits / self.clips if self.clips else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "edited": self.edited,
            "acceptance_rate": round(self.acceptance_rate, 4),
            "deletion_rate": round(self.deletion_rate, 4),
            "edits_per_clip": round(self.edits_per_clip, 4),
            "selected_kept": self.selected_kept,
            "selected_deleted": self.selected_deleted,
            "rejected_restored": self.rejected_restored,
            "marked_accepted": self.marked_accepted,
            "manual_edits": self.manual_edits,
            "clips": self.clips,
        }


def measure_project(database: Database, project_id: str) -> UserEditMetrics:
    """Read §119's numbers out of what the project already stores."""
    clips = database.fetch_all(
        "SELECT enabled FROM timeline_clips WHERE project_id = ? AND track = 'video'",
        (project_id,),
    )
    kept = sum(1 for row in clips if row["enabled"])
    deleted = len(clips) - kept

    states = database.fetch_all(
        "SELECT user_state, COUNT(*) AS count FROM moments "
        "WHERE project_id = ? GROUP BY user_state",
        (project_id,),
    )
    by_state = {str(row["user_state"]): int(row["count"]) for row in states}

    # A restored moment is one the person marked accepted after the pipeline
    # had put it below the cut -- so it is accepted *and* has no enabled clip.
    restored = database.fetch_one(
        "SELECT COUNT(*) AS count FROM moments m "
        "WHERE m.project_id = ? AND m.user_state = ? AND NOT EXISTS ("
        "  SELECT 1 FROM timeline_clips c "
        "  WHERE c.moment_id = m.id AND c.enabled = 1"
        ")",
        (project_id, ACCEPTED),
    )

    versions = database.fetch_one(
        "SELECT COUNT(*) AS count FROM edit_versions WHERE project_id = ?", (project_id,)
    )
    # Each command writes a "before" snapshot and an "after" one, so the number
    # of commands is half the versions. Counting versions would double every
    # edit and make the per-clip rate meaningless.
    version_count = int(versions["count"]) if versions else 0
    manual_edits = version_count // 2

    metrics = UserEditMetrics(
        project_id=project_id,
        selected_kept=kept,
        selected_deleted=deleted,
        rejected_restored=int(restored["count"]) if restored else 0,
        marked_accepted=by_state.get(ACCEPTED, 0),
        manual_edits=manual_edits,
        clips=len(clips),
        edited=manual_edits > 0 or deleted > 0 or by_state.get(AUTO, 0) < sum(by_state.values()),
    )
    logger.info("Measured user edits", extra=metrics.as_dict())
    return metrics


def aggregate(metrics: list[UserEditMetrics]) -> dict[str, Any]:
    """Roll several projects into one set of numbers (§119).

    Projects nobody edited are counted but excluded from the rates: including
    them would drive acceptance toward 1.0 in proportion to how many videos
    were made and never touched, which is a usage statistic wearing a quality
    statistic's clothes.
    """
    edited = [item for item in metrics if item.edited]
    if not edited:
        return {
            "projects": len(metrics),
            "edited_projects": 0,
            "note": "no project has been edited yet; §119 has nothing to measure",
        }

    kept = sum(item.selected_kept for item in edited)
    deleted = sum(item.selected_deleted for item in edited)
    total = kept + deleted
    return {
        "projects": len(metrics),
        "edited_projects": len(edited),
        "acceptance_rate": round(kept / total, 4) if total else 1.0,
        "deletion_rate": round(deleted / total, 4) if total else 0.0,
        "rejected_restored": sum(item.rejected_restored for item in edited),
        "manual_edits": sum(item.manual_edits for item in edited),
        "edits_per_clip": round(
            sum(item.manual_edits for item in edited) / sum(item.clips for item in edited), 4
        )
        if sum(item.clips for item in edited)
        else 0.0,
    }


__all__ = [
    "ACCEPTED",
    "AUTO",
    "REJECTED",
    "UserEditMetrics",
    "aggregate",
    "measure_project",
]
