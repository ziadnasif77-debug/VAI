"""Path resolution (SPEC sections 43, 91).

Every directory the application touches is derived here from the data root. No
other module builds an application path by string concatenation, and no path is
hardcoded -- gameplay recordings are large and users routinely keep projects on
a separate drive.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.config.schema import AppConfig

#: Environment variable that overrides the data root.
ROOT_ENV_VAR: Final[str] = "VAI_ROOT"

#: Files whose presence identifies the repository root during development.
_ROOT_MARKERS: Final[tuple[str, ...]] = ("pyproject.toml", "config")

#: Per-project subdirectories required by §43.
PROJECT_SUBDIRECTORIES: Final[tuple[str, ...]] = (
    "source",
    "proxy",
    "audio",
    "frames",
    "analysis",
    "events",
    "moments",
    "transcript",
    "timeline",
    "assets",
    "renders",
    "previews",
    "logs",
)

PROJECT_MANIFEST_FILENAME: Final[str] = "project.json"


def find_repository_root(start: Path | None = None) -> Path:
    """Locate the repository root.

    Walks up from ``start`` (or this file) looking for every marker in
    :data:`_ROOT_MARKERS`, falling back to the package's grandparent for
    installed layouts.
    """
    origin = Path(start) if start is not None else Path(__file__).resolve()
    if origin.is_file():
        origin = origin.parent
    for candidate in [origin, *origin.parents]:
        if all((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Filesystem layout of a single project (§43)."""

    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / PROJECT_MANIFEST_FILENAME

    @property
    def source(self) -> Path:
        """Imported recordings. Written once at import, never modified (§42)."""
        return self.root / "source"

    @property
    def proxy(self) -> Path:
        return self.root / "proxy"

    @property
    def audio(self) -> Path:
        return self.root / "audio"

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def analysis(self) -> Path:
        """Cached stage artefacts, keyed per §48."""
        return self.root / "analysis"

    @property
    def events(self) -> Path:
        return self.root / "events"

    @property
    def moments(self) -> Path:
        return self.root / "moments"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript"

    @property
    def timeline(self) -> Path:
        return self.root / "timeline"

    @property
    def assets(self) -> Path:
        """User-provided assets: local music, overlays, intro/outro (§73)."""
        return self.root / "assets"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    @property
    def previews(self) -> Path:
        """Thumbnails for scenes, events, moments and clips (§56)."""
        return self.root / "previews"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def all_directories(self) -> tuple[Path, ...]:
        """Every directory that must exist for the project to be usable."""
        return (self.root, *(self.root / name for name in PROJECT_SUBDIRECTORIES))

    def create(self) -> ProjectPaths:
        """Create the full directory tree. Idempotent."""
        for directory in self.all_directories():
            directory.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True, slots=True)
class Paths:
    """Application-wide directory layout."""

    root: Path
    config_dir: Path
    data_root: Path
    projects_dir: Path
    models_dir: Path
    logs_dir: Path
    cache_dir: Path
    profiles_dir: Path
    #: Where a person puts recordings, and where they collect finished videos.
    #: The pipeline never works in either: a project owns its own tree (§43).
    input_dir: Path
    output_dir: Path
    database_path: Path

    def project(self, project_id: str) -> ProjectPaths:
        """Return the §43 layout for ``project_id``."""
        return ProjectPaths(self.projects_dir / project_id)

    def game_profile(self, game: str) -> Path:
        """Return the directory of a game profile (§22)."""
        return self.profiles_dir / game

    def create(self) -> Paths:
        """Create the application directories. Idempotent."""
        for directory in (
            self.data_root,
            self.projects_dir,
            self.models_dir,
            self.logs_dir,
            self.cache_dir,
            self.input_dir,
            self.output_dir,
            self.database_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def _resolve(base: Path, value: str) -> Path:
    """Resolve ``value`` against ``base`` unless it is already absolute."""
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else base / candidate


def build_paths(
    config: AppConfig,
    *,
    root: Path | None = None,
    data_root: Path | None = None,
) -> Paths:
    """Derive the full path layout from configuration.

    Args:
        config: validated application configuration.
        root: repository root. Defaults to :func:`find_repository_root`.
        data_root: overrides both ``root`` and ``application.data_root``. Tests
            pass a temporary directory so no test writes into a real project.
    """
    repository_root = Path(root).resolve() if root is not None else find_repository_root()

    if data_root is not None:
        resolved_data_root = Path(data_root).resolve()
    elif config.application.data_root:
        resolved_data_root = _resolve(repository_root, config.application.data_root).resolve()
    else:
        resolved_data_root = repository_root

    directories = config.application.directories
    return Paths(
        root=repository_root,
        config_dir=repository_root / "config",
        data_root=resolved_data_root,
        projects_dir=_resolve(resolved_data_root, directories.projects),
        models_dir=_resolve(resolved_data_root, directories.models),
        logs_dir=_resolve(resolved_data_root, config.application.logging.directory),
        cache_dir=_resolve(resolved_data_root, directories.cache),
        # Game profiles ship with the code, so they resolve against the
        # repository root rather than the (possibly relocated) data root.
        profiles_dir=_resolve(repository_root, directories.profiles),
        input_dir=_resolve(resolved_data_root, directories.input),
        output_dir=_resolve(resolved_data_root, directories.output),
        database_path=_resolve(resolved_data_root, config.application.database.filename),
    )


def project_paths(projects_dir: Path, project_id: str) -> ProjectPaths:
    """Return the §43 layout for ``project_id`` under ``projects_dir``."""
    return ProjectPaths(Path(projects_dir) / project_id)


__all__ = [
    "PROJECT_MANIFEST_FILENAME",
    "PROJECT_SUBDIRECTORIES",
    "ROOT_ENV_VAR",
    "Paths",
    "ProjectPaths",
    "build_paths",
    "find_repository_root",
    "project_paths",
]
