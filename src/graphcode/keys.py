"""Join key shared by all stages: ``module.path:Qualname``.

Every node in ``graph.json`` and every step in a trace file is identified by
the same key, so the stages can be joined without knowing each other.

Examples
--------
- function or class: ``datagenerator.dag:DAG.add_node``
- nested function:   ``datagenerator.generator:timed.<locals>.wrapper``
- module or package: ``datagenerator.dag`` (no colon, no qualified name)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

MODULE_SEPARATOR = ":"

# Names Python itself inserts into qualified names. "<module>" is deliberately
# absent: a module is keyed by its dotted path alone, without a qualname.
GENERATED_QUALNAME_PARTS = frozenset(
    {"<locals>", "<lambda>", "<genexpr>", "<listcomp>", "<setcomp>", "<dictcomp>"}
)


class InvalidKeyError(ValueError):
    """Raised when a string or its parts do not form a valid join key."""


def _is_valid_module_path(module: str) -> bool:
    """Return True for a dotted path of identifiers, e.g. ``"pkg.sub.mod"``."""
    return all(part.isidentifier() for part in module.split("."))


def _is_valid_qualname(qualname: str) -> bool:
    """Return True for a dotted qualified name such as ``"f.<locals>.inner"``.

    A ``<locals>`` part must sit between two names, as Python writes it.
    """
    parts = qualname.split(".")
    if parts[0] == "<locals>" or parts[-1] == "<locals>":
        return False
    return all(part.isidentifier() or part in GENERATED_QUALNAME_PARTS for part in parts)


@dataclass(frozen=True, order=True)
class NodeKey:
    """Identity of a module, class or function across all stages.

    Attributes
    ----------
    module : str
        Dotted import path of the module, e.g. ``"datagenerator.dag"``.
    qualname : str
        Qualified name inside the module, as Python reports it in
        ``__qualname__`` / ``co_qualname``. Empty for the module itself.
    """

    module: str
    qualname: str = ""

    def __post_init__(self) -> None:
        """Validate both parts, so an invalid key can never be created."""
        if not _is_valid_module_path(self.module):
            raise InvalidKeyError(f"invalid module path: {self.module!r}")
        if self.qualname and not _is_valid_qualname(self.qualname):
            raise InvalidKeyError(f"invalid qualified name: {self.qualname!r}")

    @property
    def is_module(self) -> bool:
        """Return True when the key names a module or package, not a definition."""
        return self.qualname == ""

    def to_string(self) -> str:
        """Return the key in its file format, e.g. ``"pkg.mod:Class.method"``."""
        if self.is_module:
            return self.module
        return f"{self.module}{MODULE_SEPARATOR}{self.qualname}"

    def __str__(self) -> str:
        """Return the same text as :meth:`to_string`."""
        return self.to_string()

    @classmethod
    def from_string(cls, text: str) -> NodeKey:
        """Parse a key written by :meth:`to_string`.

        Parameters
        ----------
        text : str
            ``"module.path:Qualname"`` or ``"module.path"``.

        Returns
        -------
        NodeKey
            The parsed key.

        Raises
        ------
        InvalidKeyError
            If the text is not a valid key.
        """
        module, separator, qualname = text.partition(MODULE_SEPARATOR)
        if separator and not qualname:
            raise InvalidKeyError(f"empty qualified name after ':' in {text!r}")
        return cls(module=module, qualname=qualname)

    def parent_key(self) -> NodeKey | None:
        """Return the key one level up: the key minus its last part.

        Returns
        -------
        NodeKey or None
            ``pkg.mod:DAG.add_node`` -> ``pkg.mod:DAG``;
            ``pkg.mod:f.<locals>.inner`` -> ``pkg.mod:f``;
            ``pkg.mod:DAG`` -> ``pkg.mod``; ``pkg.mod`` -> ``pkg``;
            ``pkg`` -> None.
        """
        if self.is_module:
            parent_module, _, _ = self.module.rpartition(".")
            return NodeKey(parent_module) if parent_module else None
        parent_qualname, _, _ = self.qualname.rpartition(".")
        # "f.<locals>.inner" -> "f.<locals>" -> "f": <locals> is not a level.
        parent_qualname = parent_qualname.removesuffix(".<locals>")
        return NodeKey(self.module, parent_qualname)

    def child(self, name: str, inside_function: bool = False) -> NodeKey:
        """Return the key of a definition nested directly inside this one.

        Parameters
        ----------
        name : str
            Name of the nested class or function (a single identifier).
        inside_function : bool
            True when this key is a function, in which case Python inserts
            ``<locals>`` into the qualified name (``outer.<locals>.inner``).
            Ignored when this key is a module.

        Returns
        -------
        NodeKey
            The key of the nested definition.

        Raises
        ------
        InvalidKeyError
            If ``name`` is not a single identifier.
        """
        if not name.isidentifier():
            raise InvalidKeyError(f"child name must be one identifier, got {name!r}")
        if self.is_module:
            return NodeKey(self.module, name)
        middle = ".<locals>." if inside_function else "."
        return NodeKey(self.module, f"{self.qualname}{middle}{name}")


def module_name_from_path(relative_path: PurePosixPath | str) -> str:
    """Convert a source file path into its dotted module name.

    Parameters
    ----------
    relative_path : PurePosixPath or str
        Path of a ``.py`` file relative to the source root, i.e. the folder
        that contains the top-level package (the project root for a flat
        layout, ``src/`` for a src layout).

    Returns
    -------
    str
        Dotted module name. A package's ``__init__.py`` maps to the package
        itself: ``"pkg/sub/__init__.py"`` -> ``"pkg.sub"``.

    Raises
    ------
    InvalidKeyError
        If the path is not a relative ``.py`` path made of valid identifiers.
    """
    path = PurePosixPath(relative_path)
    if path.is_absolute() or path.suffix != ".py":
        raise InvalidKeyError(f"expected a relative .py path, got {str(path)!r}")
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    module = ".".join(parts)
    if not parts or not _is_valid_module_path(module):
        raise InvalidKeyError(f"path does not map to a module name: {str(path)!r}")
    return module


def is_inside_package(module: str, package: str) -> bool:
    """Return True if ``module`` is ``package`` itself or one of its submodules.

    Parameters
    ----------
    module : str
        Dotted module name to test, e.g. ``"datagenerator.dag"``.
    package : str
        Dotted name of the analysed package, e.g. ``"datagenerator"``.

    Returns
    -------
    bool
        True for ``"datagenerator"`` and ``"datagenerator.dag"``, False for
        ``"datagenerator_extra"``.
    """
    return module == package or module.startswith(package + ".")
