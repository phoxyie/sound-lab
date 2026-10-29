"""Projects: each has its own inputs/, outputs/, voice-records/ folders → projects/<name>/

The current project is kept in projects/.current; the CLI and GUI share it.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = Path(os.environ.get("SOUNDLAB_PROJECTS", ROOT / "projects"))
CURRENT_FILE = PROJECTS_ROOT / ".current"
DEFAULT_NAME = "Default"
LEGACY_DIRS = ("inputs", "outputs", "voice-records")  # root folders from before projects existed
NAME_MAX = 60


class ProjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class Project:
    name: str

    @property
    def root(self) -> Path:
        return PROJECTS_ROOT / self.name

    @property
    def inputs(self) -> Path:
        return self.root / "inputs"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def records(self) -> Path:
        return self.root / "voice-records"

    @property
    def dirs(self) -> list[Path]:
        return [self.inputs, self.outputs, self.records]


def clean_name(name: str) -> str:
    name = "".join(ch for ch in (name or "") if ch not in '/\\:\0').strip().lstrip(".")
    if not name:
        raise ProjectError("Project name can't be empty")
    if len(name) > NAME_MAX:
        raise ProjectError(f"Project name can be at most {NAME_MAX} characters")
    return name


def list_projects() -> list[str]:
    ensure_projects()
    return sorted((p.name for p in PROJECTS_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=str.casefold)


def ensure_projects() -> None:
    """On first use, moves the root folders into the DEFAULT_NAME project; creates an empty one if none exist."""
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    if any(p.is_dir() and not p.name.startswith(".") for p in PROJECTS_ROOT.iterdir()):
        return
    default = Project(DEFAULT_NAME)
    default.root.mkdir()
    for d in LEGACY_DIRS:
        legacy = ROOT / d
        if legacy.is_dir() and PROJECTS_ROOT.resolve() == (ROOT / "projects").resolve():
            shutil.move(str(legacy), str(default.root / d))
    for d in default.dirs:
        d.mkdir(exist_ok=True)


def current() -> Project:
    names = list_projects()
    try:
        name = CURRENT_FILE.read_text().strip()
    except OSError:
        name = ""
    if name not in names:
        name = DEFAULT_NAME if DEFAULT_NAME in names else names[0]
        CURRENT_FILE.write_text(name)
    return Project(name)


def set_current(name: str) -> Project:
    if name not in list_projects():
        raise ProjectError(f"Project not found: {name}")
    CURRENT_FILE.write_text(name)
    return Project(name)


def create(name: str) -> Project:
    name = clean_name(name)
    if name.casefold() in (n.casefold() for n in list_projects()):
        raise ProjectError(f"A project with this name already exists: {name}")
    project = Project(name)
    for d in project.dirs:
        d.mkdir(parents=True)
    return set_current(name)


def rename(name: str, new_name: str) -> Project:
    if name not in list_projects():
        raise ProjectError(f"Project not found: {name}")
    new_name = clean_name(new_name)
    if new_name == name:
        return Project(name)
    if any(n != name and n.casefold() == new_name.casefold() for n in list_projects()):
        raise ProjectError(f"A project with this name already exists: {new_name}")
    was_current = current().name == name
    Project(name).root.rename(Project(new_name).root)  # same volume: a plain rename, case-only changes work too
    if was_current:
        CURRENT_FILE.write_text(new_name)
    return Project(new_name)


def delete(name: str, trash_dir: Path) -> Project:
    """Moves the project folder to the Trash; if it was current, switches to another (creates one if needed)."""
    if name not in list_projects():
        raise ProjectError(f"Project not found: {name}")
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / name
    n = 2
    while dest.exists():
        dest = trash_dir / f"{name}-{n}"
        n += 1
    shutil.move(str(Project(name).root), str(dest))
    # Last project deleted: list_projects() creates an empty DEFAULT_NAME one; current() picks a valid project
    return current()
