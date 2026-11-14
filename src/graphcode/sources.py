"""Find a package's source files and read them, from the working tree or from a git commit.

A commit is read straight from git (``git ls-tree`` lists its files,
``git show <commit>:./<path>`` gives one file's text): nothing is checked out
and no code runs, so any commit can be analysed while the working tree stays
untouched. Both sources share one rule for which files form the package.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Collection, Sequence
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
    """Locate ``src/<package>`` or ``<package>`` in the working tree and list its ``.py`` files.

    Raises
    ------
    SourceError
        If neither folder holds an ``__init__.py``.
    """
    candidates = [
        path.relative_to(project_root).as_posix()
        for folder in (project_root / "src" / package, project_root / package)
        for path in folder.rglob("*.py")
    ]
    return _package_layout(candidates, package, exclude, str(project_root))


def find_package_files_at(project_root: Path, commit: str, package: str, exclude: Sequence[str] = ()) -> PackageLayout:
    """Like :func:`find_package_files`, for the files of a git commit.

    Raises
    ------
    SourceError
        If the commit has no such package.
    """
    # Run in the project folder: paths come out relative to it, even inside a larger repository.
    listing = _git(project_root, "ls-tree", "-r", "--name-only", commit, "--", f"src/{package}", package)
    return _package_layout(listing.splitlines(), package, exclude, f"commit {commit[:7]}")


def _package_layout(candidates: Collection[str], package: str, exclude: Sequence[str], where: str) -> PackageLayout:
    """Pick the package's ``.py`` files among project-relative paths; ``src/`` layout first.

    Raises
    ------
    SourceError
        If neither ``src/<package>/__init__.py`` nor ``<package>/__init__.py`` is among them.
    """
    for source_root in ("src", ""):
        prefix = f"{source_root}/{package}/".lstrip("/")
        if f"{prefix}__init__.py" in candidates:
            break
    else:
        raise SourceError(f"package {package!r} not found in {where} (looked in src/ and the root)")
    files = [
        path for path in candidates
        if path.startswith(prefix) and path.endswith(".py")
        and not any(path == excluded or path.startswith(f"{excluded}/") for excluded in exclude)
    ]
    return PackageLayout(source_root, tuple(sorted(files)))


def working_tree_reader(project_root: Path) -> SourceReader:
    """Return a reader of files on disk, relative to ``project_root``."""

    def read_source(relative_path: str) -> str:
        """Return the UTF-8 text of one project file."""
        return (project_root / relative_path).read_text(encoding="utf-8")

    return read_source


def commit_reader(project_root: Path, commit: str) -> SourceReader:
    """Return a reader of files as they are in a git commit, relative to ``project_root``."""

    def read_source(relative_path: str) -> str:
        """Return the UTF-8 text of one project file at the commit."""
        # "./" makes the path relative to the project folder, not to the repository root.
        completed = subprocess.run(
            ["git", "show", f"{commit}:./{relative_path}"], cwd=project_root, capture_output=True, check=True
        )
        return completed.stdout.decode("utf-8")

    return read_source


def resolve_commit(project_root: Path, ref: str) -> str:
    """Return the full commit hash of a branch, tag or commit name.

    Raises
    ------
    SourceError
        If git does not know ``ref`` (or the project is not in git).
    """
    try:
        return _git(project_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except (OSError, subprocess.CalledProcessError) as error:
        raise SourceError(f"unknown git ref {ref!r} in {project_root}") from error


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
