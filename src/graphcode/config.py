"""Read the ``[tool.graphcode]`` section of a project's ``pyproject.toml``.

Milestone 1 reads only ``package`` (required) and ``exclude`` (optional)::

    [tool.graphcode]
    package = "datagenerator"
    exclude = ["datagenerator/legacy"]
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib; only reading the config needs it
    tomllib = None  # type: ignore[assignment]

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
    """

    project_root: Path
    package: str
    exclude: tuple[str, ...] = ()

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
    if tomllib is None:
        raise ConfigError("reading pyproject.toml needs Python 3.11 or later")
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
    return GraphCodeConfig(project_root, package, tuple(path.strip("/") for path in exclude))
