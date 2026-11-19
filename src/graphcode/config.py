"""Read the ``[tool.graphcode]`` section of a project's ``pyproject.toml``.

Reads ``package`` (required unless given with ``--package``), ``exclude`` and ``scenarios`` (optional)::

    [tool.graphcode]
    package = "datagenerator"
    exclude = ["datagenerator/legacy"]

    [tool.graphcode.scenarios]
    sample-small = "scripts.scenarios:sample_small"

Scenarios are passed on as written; ``trace.targets`` interprets them.

The command line can override two settings, to analyse a project without
touching it: ``--package`` (then the section, and even ``pyproject.toml``,
may be absent) and ``--output`` (instead of ``<project>/.graphcode/``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: tomli is the same parser, installed only there
    import tomli as tomllib

OUTPUT_FOLDER = ".graphcode"


class ConfigError(Exception):
    """Raised when the project configuration is missing or invalid."""


@dataclass(frozen=True)
class GraphCodeConfig:
    """Configuration of one analysed project.

    Attributes
    ----------
    project_root : Path
        Folder of the analysed project (usually holding ``pyproject.toml``).
    package : str
        Import name of the package to graph (top level, no dots).
    exclude : tuple of str
        Paths relative to the project root whose files are not analysed.
    scenarios : dict of str to Any
        The ``[tool.graphcode.scenarios]`` table, name -> target, as written.
    chosen_output_folder : Path or None
        Absolute folder given with ``--output``; None means ``<project>/.graphcode/``.
    """

    project_root: Path
    package: str
    exclude: tuple[str, ...] = ()
    scenarios: dict[str, Any] = field(default_factory=dict)
    chosen_output_folder: Path | None = None

    @property
    def output_folder(self) -> Path:
        """Folder where every generated file goes (``.graphcode/`` unless chosen)."""
        if self.chosen_output_folder is not None:
            return self.chosen_output_folder
        return self.project_root / OUTPUT_FOLDER


def load_config(
    project_root: Path, package: str | None = None, output_folder: Path | None = None
) -> GraphCodeConfig:
    """Read the configuration from ``project_root/pyproject.toml``.

    Parameters
    ----------
    project_root : Path
        Folder of the analysed project.
    package : str or None
        Overrides ``[tool.graphcode] package``; when given, the section and
        ``pyproject.toml`` are optional (``exclude`` and ``scenarios`` are
        still read if present).
    output_folder : Path or None
        Overrides ``<project>/.graphcode/``; a relative path is taken from
        the current folder, not from the project.

    Returns
    -------
    GraphCodeConfig
        The settings, with the overrides applied.

    Raises
    ------
    ConfigError
        If the file or the section is missing (and no ``package`` was given),
        or a setting is invalid.
    """
    pyproject = project_root / "pyproject.toml"
    settings = _read_section(pyproject)
    if settings is None and package is None:
        if not pyproject.is_file():
            raise ConfigError(f"no pyproject.toml in {project_root}: add one, or pass --package <import name>")
        raise ConfigError(
            f'{pyproject} has no [tool.graphcode] section; add: package = "<import name>", or pass --package'
        )
    settings = settings or {}
    chosen_package = settings.get("package") if package is None else package
    if chosen_package is None:
        raise ConfigError('[tool.graphcode] has no package; add: package = "<import name>", or pass --package')
    if not isinstance(chosen_package, str) or not chosen_package.isidentifier():
        raise ConfigError(f"package must be a top-level import name, got {chosen_package!r}")
    exclude = settings.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(path, str) for path in exclude):
        raise ConfigError("[tool.graphcode] exclude must be a list of paths")
    scenarios = settings.get("scenarios", {})
    if not isinstance(scenarios, dict):
        raise ConfigError("[tool.graphcode.scenarios] must be a table of name = target")
    return GraphCodeConfig(
        project_root=project_root,
        package=chosen_package,
        exclude=tuple(path.strip("/") for path in exclude),
        scenarios=scenarios,
        chosen_output_folder=None if output_folder is None else output_folder.resolve(),
    )


def _read_section(pyproject: Path) -> dict[str, Any] | None:
    """Return the ``[tool.graphcode]`` table, or None if the file or the section is absent.

    Parameters
    ----------
    pyproject : Path
        Path of the project's ``pyproject.toml`` (it may not exist).

    Returns
    -------
    dict of str to Any, or None
        The section's settings as parsed.

    Raises
    ------
    ConfigError
        If the file exists but is not valid TOML.
    """
    if not pyproject.is_file():
        return None
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("graphcode")
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"cannot read {pyproject}: {error}") from error
