"""Extract definition nodes (packages, modules, classes, functions) with ``ast``.

This pass produces the nodes of the structure graph, without edges. Rules:

- each ``__init__.py`` is a package node; every other file is a module node
  spanning the whole file;
- each ``class`` and ``def`` (including ``async def``) is a node; a ``def``
  directly inside a class is a method, any other ``def`` is a function;
- definitions inside ``if``/``try``/``with``/``for``/``match`` blocks belong to
  the enclosing scope (e.g. under ``if TYPE_CHECKING:``);
- lambdas and comprehensions are expressions, never nodes;
- a definition's span starts at its first decorator;
- a key defined several times (property setter, ``@overload``, conditional
  definitions) becomes one node with several spans; the last kind wins, as
  the last definition is the one that exists at run time.

The package is assumed to be well structured: pathological code (``global``
definitions inside functions, folders without ``__init__.py``) gets a
reasonable default, not special handling.
"""

from __future__ import annotations

import ast
import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from graphcode.graph.model import LineSpan, Node, NodeKind
from graphcode.keys import InvalidKeyError, NodeKey, module_name_from_path

SourceReader = Callable[[str], str]
"""Function returning the text of a file, given its project-relative path."""

_DefinitionStatement = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


class ExtractionError(Exception):
    """Raised when a source file cannot be named as a module or parsed."""


@dataclass
class _NodeDraft:
    """Mutable node under construction, before duplicates are merged."""

    kind: NodeKind
    file: str
    parent: NodeKey | None
    spans: list[LineSpan] = field(default_factory=list)


def extract_definitions(
    file_paths: Sequence[str],
    read_source: SourceReader,
    source_root: str = "",
) -> tuple[Node, ...]:
    """Return one node per package, module, class and function in the files.

    Parameters
    ----------
    file_paths : sequence of str
        ``.py`` files to analyse, relative to the project root, with ``/``
        separators (e.g. ``"datagenerator/dag.py"``).
    read_source : SourceReader
        Returns a file's text from its path; reads the disk or a git ref.
    source_root : str
        Folder, relative to the project root, that contains the top-level
        package: ``""`` for a flat layout, ``"src"`` for a src layout.

    Returns
    -------
    tuple of Node
        Nodes sorted by key, with duplicate keys merged.

    Raises
    ------
    ExtractionError
        If a file has a syntax error, is not a valid module name, lies
        outside ``source_root``, or two files map to the same module
        (``pkg/mod.py`` and ``pkg/mod/__init__.py``).
    """
    module_by_file = {path: _module_name(path, source_root) for path in file_paths}
    _reject_module_clashes(module_by_file)
    package_names = {module for path, module in module_by_file.items() if _is_package_file(path)}
    drafts: dict[NodeKey, _NodeDraft] = {}

    for path in sorted(file_paths):
        text = read_source(path)
        module_key = NodeKey(module_by_file[path])
        # Only a real package (with __init__.py) can contain a module. A
        # module without one (namespace folder) is a root.
        enclosing_package = module_key.parent_key()
        if enclosing_package is not None and enclosing_package.module not in package_names:
            enclosing_package = None
        drafts[module_key] = _NodeDraft(
            kind=NodeKind.PACKAGE if _is_package_file(path) else NodeKind.MODULE,
            file=path,
            parent=enclosing_package,
            spans=[LineSpan(1, _line_count(text))],
        )
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as error:
            raise ExtractionError(f"cannot parse {path}: {error}") from error
        _collect_scope(tree.body, module_key, NodeKind.MODULE, path, drafts)

    return tuple(
        Node(key=key, kind=draft.kind, file=draft.file, spans=tuple(draft.spans), parent=draft.parent)
        for key, draft in sorted(drafts.items())
    )


def _module_name(path: str, source_root: str) -> str:
    """Return the dotted module name of a project-relative file path."""
    try:
        relative = PurePosixPath(path)
        if source_root:
            relative = relative.relative_to(source_root)
        return module_name_from_path(relative)
    # relative_to raises ValueError for a file outside source_root.
    except (ValueError, InvalidKeyError) as error:
        raise ExtractionError(f"cannot name {path} as a module: {error}") from error


def _reject_module_clashes(module_by_file: dict[str, str]) -> None:
    """Raise if two files map to the same module (``mod.py`` and ``mod/__init__.py``)."""
    file_by_module: dict[str, str] = {}
    for path, module in sorted(module_by_file.items()):
        if module in file_by_module:
            raise ExtractionError(f"{file_by_module[module]} and {path} both define module {module}")
        file_by_module[module] = path


def _is_package_file(path: str) -> bool:
    """Return True for a package's ``__init__.py``."""
    return PurePosixPath(path).name == "__init__.py"


def _line_count(text: str) -> int:
    """Count lines as Python's parser does, at least 1 for an empty file.

    ``str.splitlines`` also splits on form feeds and other control characters
    that the parser does not treat as line breaks; ``io.StringIO`` splits
    only on ``\\n``, ``\\r\\n`` and ``\\r``.
    """
    return max(1, len(io.StringIO(text).readlines()))


def _collect_scope(
    body: list[ast.stmt],
    scope_key: NodeKey,
    scope_kind: NodeKind,
    path: str,
    drafts: dict[NodeKey, _NodeDraft],
) -> None:
    """Record every definition of one scope, then recurse into each of them.

    Parameters
    ----------
    body : list of ast.stmt
        Statements of the module, class or function body.
    scope_key : NodeKey
        Key of the enclosing module, class or function.
    scope_kind : NodeKind
        Kind of the enclosing scope; decides method vs function and whether
        Python inserts ``<locals>`` into nested qualified names.
    path : str
        File being analysed.
    drafts : dict
        Nodes collected so far; updated in place.
    """
    inside_function = scope_kind in (NodeKind.FUNCTION, NodeKind.METHOD)
    for statement in _statements_of_scope(body):
        key = scope_key.child(statement.name, inside_function=inside_function)
        kind = _definition_kind(statement, scope_kind)
        span = LineSpan(_first_line(statement), statement.end_lineno or statement.lineno)
        draft = drafts.setdefault(key, _NodeDraft(kind=kind, file=path, parent=scope_key))
        draft.kind = kind  # a redefinition replaces the earlier one: last kind wins
        draft.spans.append(span)
        _collect_scope(statement.body, key, kind, path, drafts)


def _statements_of_scope(body: list[ast.stmt]) -> Iterator[_DefinitionStatement]:
    """Yield the definitions of a scope, looking inside non-scope blocks.

    ``if``, ``try``, ``with``, ``for``, ``while`` and ``match`` do not create a
    new scope in Python, so their nested statements belong to this scope.
    Class and function bodies do create one and are not entered here.
    """
    for statement in body:
        if isinstance(statement, _DefinitionStatement):
            yield statement
            continue
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                yield from _statements_of_scope([child])
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                yield from _statements_of_scope(child.body)


def _definition_kind(statement: _DefinitionStatement, scope_kind: NodeKind) -> NodeKind:
    """Return CLASS, METHOD (a def directly in a class) or FUNCTION."""
    if isinstance(statement, ast.ClassDef):
        return NodeKind.CLASS
    if scope_kind == NodeKind.CLASS:
        return NodeKind.METHOD
    return NodeKind.FUNCTION


def _first_line(statement: _DefinitionStatement) -> int:
    """Return the line of the first decorator, or of the definition itself."""
    return min([statement.lineno] + [decorator.lineno for decorator in statement.decorator_list])
