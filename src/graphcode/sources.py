"""Find a package's source files and read them from the working tree.

Reading files at another git ref (``git show``) comes with the PR diff,
milestone 2.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

SourceReader = Callable[[str], str]
"""Function returning the text of a file, given its project-relative path."""


class SourceError(Exception):
    """Raised when the package cannot be found in the project."""


@dataclass(frozen=True)
class PackageLayout:
    """Where a package lives and which files it has.

    Attributes
    ----------
    source_root : str
        ``"src"`` for a src layout, ``""`` for a flat layout.
    files : tuple of str
        ``.py`` files of the package, relative to the project root, sorted.
    """

    source_root: str
    files: tuple[str, ...]


def find_package_files(project_root: Path, package: str, exclude: Sequence[str] = ()) -> PackageLayout:
    """Locate ``src/<package>`` or ``<package>`` and list its ``.py`` files.

    Raises
    ------
    SourceError
        If neither folder holds an ``__init__.py``.
    """
    for source_root in ("src", ""):
        package_folder = project_root / source_root / package
        if (package_folder / "__init__.py").is_file():
            break
    else:
        raise SourceError(f"package {package!r} not found in {project_root} (looked in src/ and the root)")
    files = []
    for path in sorted(package_folder.rglob("*.py")):
        relative = path.relative_to(project_root).as_posix()
        if not any(relative == excluded or relative.startswith(f"{excluded}/") for excluded in exclude):
            files.append(relative)
    return PackageLayout(source_root, tuple(files))


def working_tree_reader(project_root: Path) -> SourceReader:
    """Return a reader of files on disk, relative to ``project_root``."""

    def read_source(relative_path: str) -> str:
        """Return the UTF-8 text of one project file."""
        return (project_root / relative_path).read_text(encoding="utf-8")

    return read_source


def git_state(project_root: Path, paths: Sequence[str] = ()) -> tuple[str | None, bool]:
    """Return ``(commit hash, dirty)``; ``(None, False)`` outside a git repository.

    ``dirty`` is True when any of ``paths`` (default: the whole repository)
    has uncommitted or untracked changes.
    """
    try:
        commit = _git(project_root, "rev-parse", "HEAD")
        changes = _git(project_root, "status", "--porcelain", "--", *paths)
    except (OSError, subprocess.CalledProcessError):
        return None, False
    return commit, bool(changes)


def _git(project_root: Path, *arguments: str) -> str:
    """Run a git command in the project and return its stripped output."""
    completed = subprocess.run(
        ["git", *arguments], cwd=project_root, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()
