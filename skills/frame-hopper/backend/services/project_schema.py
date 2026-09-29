"""Canonical project storage schema constants and metadata model.

All consumers (services, routers, tests) MUST import from here rather
than hardcoding subfolder strings. Layout matches docs/PROJECT_SCHEMA.md.
"""
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

STORAGE_ROOT_DIRNAME = "projects"
ARCHIVE_DIRNAME = "_archive"
TEMPLATE_DIRNAME = "_template"

INPUTS_DIRNAME = "inputs"
EXTENDED_DIRNAME = "extended"
PROMPTS_DIRNAME = "prompts"
CLIPS_DIRNAME = "clips"
CLIPS_RAW_DIRNAME = "raw"
CLIPS_SELECTED_DIRNAME = "selected"
AUDIO_DIRNAME = "audio"
FINAL_DIRNAME = "final"
EXPORTS_DIRNAME = "exports"
METADATA_DIRNAME = "metadata"
LOGS_DIRNAME = "logs"
FAMILY_PIPELINE_DIRNAME = "family_pipeline"
PREPARED_SOURCES_DIRNAME = "prepared_sources"

ProjectStatus = Literal["draft", "in_progress", "review", "delivered", "archived"]


class ProjectMeta(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    created_at: date
    status: ProjectStatus = "draft"
    tags: list[str] = Field(default_factory=list)
    audio_track: str | None = None
    source: str = "operator_upload"


def project_root(storage_root: Path, user_id: str, slug: str) -> Path:
    """Return the canonical path to a project's folder.

    Operator-driven phase (`user_id == "local"`): `{storage_root}/{slug}/`.
    Multi-user phase: `{storage_root}/{user_id}/{slug}/`.
    """
    user_id = validate_project_component(user_id, label="user id")
    slug = validate_project_component(slug, label="project id")
    if user_id == "local":
        return storage_root / slug
    return storage_root / user_id / slug


class ProjectPathError(ValueError):
    """A project identifier or resolved project path is unsafe."""


class AmbiguousProjectPathError(ProjectPathError):
    """Both the canonical and historical local project directories exist."""


_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def validate_project_component(value: str, *, label: str) -> str:
    """Accept exactly one ordinary filesystem component on every platform."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ProjectPathError(f"invalid {label}")
    path = Path(value)
    if (
        path.is_absolute()
        or len(path.parts) != 1
        or "/" in value
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in '<>"|?*' for character in value)
        or value.rstrip(". ") != value
    ):
        raise ProjectPathError(f"invalid {label}")
    # Windows treats e.g. CON.txt as the CON device; reject these aliases on
    # every host so a project id has one portable meaning.
    if value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise ProjectPathError(f"invalid {label}")
    return value


def _path_present(path: Path) -> bool:
    """Treat broken links as present so they cannot silently become a new root."""
    return os.path.lexists(path)


def _linked(path: Path) -> bool:
    """Identify symlinks and Windows junctions without following either."""
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _assert_no_project_links(storage_root: Path, candidate: Path) -> None:
    """Reject links below the declared root before resolving a project path.

    A link to another directory *inside* storage is still unsafe: it lets one
    API project alias another user's or local project's data.
    """
    lexical_root = storage_root.absolute()
    lexical_candidate = candidate.absolute()
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise ProjectPathError("project path escapes storage root") from exc
    current = lexical_root
    for component in relative.parts:
        current = current / component
        if _path_present(current) and _linked(current):
            raise ProjectPathError("project path contains a symlink or junction")


def _contained_resolved_path(storage_root: Path, candidate: Path) -> Path:
    """Resolve parent links and reject a project directory outside storage_root."""
    _assert_no_project_links(storage_root, candidate)
    root = storage_root.resolve(strict=False)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectPathError("project path escapes storage root") from exc
    if _path_present(candidate) and (not candidate.exists() or not candidate.is_dir()):
        raise ProjectPathError("project path is not a directory")
    return resolved


def resolve_api_project_directory(storage_root: Path, user_id: str, project_id: str) -> Path:
    """Resolve an API project's safe directory without migrating user files.

    Local projects use ``{root}/{id}``.  A historical ``{root}/local/{id}``
    directory is read only as a fallback when the canonical directory is
    absent.  Coexisting paths are deliberately ambiguous: callers must not
    merge, move, or choose between existing user data.
    """
    user_id = validate_project_component(user_id, label="user id")
    project_id = validate_project_component(project_id, label="project id")
    storage_root = Path(storage_root)
    canonical = project_root(storage_root, user_id, project_id)
    if user_id != "local":
        return _contained_resolved_path(storage_root, canonical)

    historical = storage_root / "local" / project_id
    canonical_present = _path_present(canonical)
    historical_present = _path_present(historical)
    if canonical_present and historical_present:
        raise AmbiguousProjectPathError(
            "canonical and historical local project directories both exist"
        )
    selected = historical if historical_present else canonical
    return _contained_resolved_path(storage_root, selected)
