"""Read the ``[tool.graphcode]`` section of a project's ``pyproject.toml``.

Reads ``package`` (required), ``exclude`` and ``scenarios`` (optional)::

    [tool.graphcode]
    package = "datagenerator"
    exclude = ["datagenerator/legacy"]

    [tool.graphcode.scenarios]
    sample-small = "scripts.scenarios:sample_small"

Scenarios are passed on as written; ``trace.targets`` interprets them.
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
        Folder holding ``pyproject.toml``.
    package : str
        Import name of the package to graph (top level, no dots).
    exclude : tuple of str
        Paths relative to the project root whose files are not analysed.
    scenarios : dict of str to Any
        The ``[tool.graphcode.scenarios]`` table, name -> target, as written.
    """

    project_root: Path
    package: str
    exclude: tuple[str, ...] = ()
    scenarios: dict[str, Any] = field(default_factory=dict)

    @property
    def output_folder(self) -> Path:
        """Folder where every generated file goes (``.graphcode/``)."""
        return self.project_root / OUTPUT_FOLDER


def load_config(project_root: Path) -> GraphCodeConfig:
    """Read the configuration from ``project_root/pyproject.toml``.

    Raises
    ------
    ConfigError
        If the file or the section is missing, or ``package`` is invalid.
    """
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        raise ConfigError(f"no pyproject.toml in {project_root}")
    try:
        settings = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("graphcode")
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"cannot read {pyproject}: {error}") from error
    if settings is None:
        raise ConfigError(f'{pyproject} has no [tool.graphcode] section; add: package = "<import name>"')
    package = settings.get("package")
    if not isinstance(package, str) or not package.isidentifier():
        raise ConfigError(f"[tool.graphcode] package must be a top-level import name, got {package!r}")
    exclude = settings.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(path, str) for path in exclude):
        raise ConfigError("[tool.graphcode] exclude must be a list of paths")
    scenarios = settings.get("scenarios", {})
    if not isinstance(scenarios, dict):
        raise ConfigError("[tool.graphcode.scenarios] must be a table of name = target")
    return GraphCodeConfig(project_root, package, tuple(path.strip("/") for path in exclude), scenarios)
