"""Extract import edges and name tables with ``ast``.

Three results, for the analysed package only:

- **Import edges**, from the importing scope to the imported module. A
  module-level import gives ``module -> module``; an import inside a function
  gives ``function -> module``, since it only runs when the function is
  called. Imports under ``if TYPE_CHECKING:`` give no edge: they never run.
  Imports of the standard library or third-party packages give no edge.
- **Name tables**, one per scope that imports something from the package:
  local name -> the node it refers to, e.g. ``"Graph" -> sample.core:Graph``.
  Names are resolved like Python does for ``from pkg import x``: first a
  definition in ``pkg``, then a name ``pkg`` re-exports (also through
  ``from .core import *``, ignoring ``__all__``), then the submodule
  ``pkg.x``. Type-checking imports are included, so annotations resolve.
- **External names**, per scope: local names bound to modules outside the
  package (``np``, ``path``). The call resolver needs them so that a local
  ``from json import load`` hides a module-level ``from pkg.io import load``.

Class-body imports are recorded under the class's key; as in Python, the
call resolver must not make them visible inside methods.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from graphcode.graph.definitions import ExtractionError, SourceReader
from graphcode.graph.model import Confidence, Edge, EdgeKind, Node, NodeKind
from graphcode.keys import NodeKey

# Bound on re-export chains (a imports from b, which imports from c, ...),
# so that a circular re-export cannot loop forever.
_MAX_REEXPORT_DEPTH = 10


@dataclass(frozen=True)
class ImportAnalysis:
    """Import edges and name tables of a package.

    Attributes
    ----------
    edges : tuple of Edge
        One ``IMPORTS`` edge per (importing scope, imported module).
    names : mapping
        Scope key (module, class or function) -> local name -> node key.
    external_names : mapping
        Scope key -> local names bound to modules outside the package.
    """

    edges: tuple[Edge, ...]
    names: Mapping[NodeKey, Mapping[str, NodeKey]]
    external_names: Mapping[NodeKey, frozenset[str]]


@dataclass(frozen=True)
class _Binding:
    """One name bound by an import statement, before resolution.

    Attributes
    ----------
    local_name : str
        Name bound in the importing scope (empty for a star import).
    module : str
        Absolute module the name comes from.
    attribute : str or None
        Imported name for ``from module import attribute``; None for
        ``import module`` (the name is the module itself) and star imports.
    depends_on : str
        Module whose loading the statement triggers: for ``import a.b.c`` the
        name is ``a`` but the dependency is ``a.b.c``.
    is_star_import : bool
        True for ``from module import *``.
    """

    local_name: str
    module: str
    attribute: str | None
    depends_on: str
    is_star_import: bool = False


@dataclass(frozen=True)
class _FoundImport:
    """An import binding together with where it was found."""

    scope_key: NodeKey
    binding: _Binding
    line: int
    type_checking_only: bool


def extract_imports(nodes: Sequence[Node], read_source: SourceReader) -> ImportAnalysis:
    """Return the import edges and name tables of the extracted package.

    Parameters
    ----------
    nodes : sequence of Node
        Nodes from :func:`graphcode.graph.definitions.extract_definitions`.
    read_source : SourceReader
        Returns a file's text from its project-relative path.

    Returns
    -------
    ImportAnalysis
        Edges sorted by (source, target), name tables and external names.
    """
    node_keys = {node.key for node in nodes}
    module_names = {node.key.module for node in nodes if node.key.is_module}

    # Pass 1: collect every binding, so that re-exports can be followed.
    found_imports: list[_FoundImport] = []
    for module_node in sorted((node for node in nodes if node.key.is_module), key=lambda node: node.key):
        tree = ast.parse(read_source(module_node.file), filename=module_node.file)
        is_package = module_node.kind == NodeKind.PACKAGE
        for scope_key, statement, type_checking_only in _imports_in_scope(tree.body, module_node.key, False, False):
            for binding in _bindings_of(statement, module_node.key.module, is_package, module_node.file):
                found_imports.append(_FoundImport(scope_key, binding, statement.lineno, type_checking_only))
    bindings_by_scope: dict[NodeKey, list[_Binding]] = {}
    for found in found_imports:
        bindings_by_scope.setdefault(found.scope_key, []).append(found.binding)

    # Pass 2: resolve each binding, then derive names and edges from it.
    names: dict[NodeKey, dict[str, NodeKey]] = {}
    external_names: dict[NodeKey, set[str]] = {}
    edge_lines: dict[tuple[NodeKey, NodeKey], list[int]] = {}
    for found in found_imports:
        binding = found.binding
        if binding.depends_on not in module_names and not _is_inside_package(binding.depends_on, module_names):
            if not binding.is_star_import:
                external_names.setdefault(found.scope_key, set()).add(binding.local_name)
            continue
        resolved = None
        if not binding.is_star_import:
            resolved = _resolve(binding.module, binding.attribute, bindings_by_scope, node_keys, module_names, 0)
            if resolved is not None:
                names.setdefault(found.scope_key, {})[binding.local_name] = resolved
        if found.type_checking_only:
            continue
        target = _dependency_target(binding, resolved, module_names)
        if target is not None and target != found.scope_key:
            edge_lines.setdefault((found.scope_key, target), []).append(found.line)

    edges = tuple(
        Edge(source, target, EdgeKind.IMPORTS, Confidence.EXACT, tuple(lines))
        for (source, target), lines in sorted(edge_lines.items())
    )
    return ImportAnalysis(
        edges=edges,
        names=names,
        external_names={scope: frozenset(local_names) for scope, local_names in external_names.items()},
    )


def _imports_in_scope(
    body: list[ast.stmt],
    scope_key: NodeKey,
    inside_function: bool,
    type_checking_only: bool,
) -> Iterator[tuple[NodeKey, ast.Import | ast.ImportFrom, bool]]:
    """Yield ``(scope key, import statement, type-checking only)`` for a body.

    Recurses into functions and classes, giving their imports the nested
    scope's key (built like :mod:`graphcode.graph.definitions` does), and into
    ``if``/``try``/``with``/... blocks, which keep the current scope. The body
    of ``if TYPE_CHECKING:`` is flagged.
    """
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            yield scope_key, statement, type_checking_only
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child_key = scope_key.child(statement.name, inside_function=inside_function)
            is_function = not isinstance(statement, ast.ClassDef)
            yield from _imports_in_scope(statement.body, child_key, is_function, type_checking_only)
        elif isinstance(statement, ast.If) and _is_type_checking_test(statement.test):
            yield from _imports_in_scope(statement.body, scope_key, inside_function, True)
            yield from _imports_in_scope(statement.orelse, scope_key, inside_function, type_checking_only)
        else:
            for child in ast.iter_child_nodes(statement):
                if isinstance(child, ast.stmt):
                    yield from _imports_in_scope([child], scope_key, inside_function, type_checking_only)
                elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                    yield from _imports_in_scope(child.body, scope_key, inside_function, type_checking_only)


def _is_type_checking_test(test: ast.expr) -> bool:
    """Return True for ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _bindings_of(
    statement: ast.Import | ast.ImportFrom,
    module: str,
    is_package: bool,
    path: str,
) -> Iterator[_Binding]:
    """Yield the names an import statement binds, with absolute module paths.

    ``import a.b.c`` binds ``a`` (the top package) unless aliased;
    ``import a.b.c as x`` binds ``x`` to ``a.b.c``.
    """
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            if alias.asname:
                yield _Binding(alias.asname, alias.name, None, depends_on=alias.name)
            else:
                top_package = alias.name.split(".")[0]
                yield _Binding(top_package, top_package, None, depends_on=alias.name)
        return
    source_module = _absolute_module(statement, module, is_package, path)
    for alias in statement.names:
        if alias.name == "*":
            yield _Binding("", source_module, None, depends_on=source_module, is_star_import=True)
        else:
            yield _Binding(alias.asname or alias.name, source_module, alias.name, depends_on=source_module)


def _absolute_module(statement: ast.ImportFrom, module: str, is_package: bool, path: str) -> str:
    """Turn ``from ..x import y`` into an absolute module path.

    Level 1 (``.``) is the package containing the module (the module itself
    for an ``__init__.py``); each extra dot goes one package up.
    """
    if statement.level == 0:
        return statement.module or ""
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    levels_up = statement.level - 1
    if levels_up >= len(package_parts):
        raise ExtractionError(f"{path}:{statement.lineno}: relative import goes above the top package")
    base_parts = package_parts[: len(package_parts) - levels_up]
    return ".".join(base_parts + ([statement.module] if statement.module else []))


def _is_inside_package(module: str, module_names: set[str]) -> bool:
    """Return True if ``module`` or one of its parent packages was extracted."""
    parts = module.split(".")
    return any(".".join(parts[:length]) in module_names for length in range(1, len(parts)))


def _dependency_target(binding: _Binding, resolved: NodeKey | None, module_names: set[str]) -> NodeKey | None:
    """Return the module an import edge points to, or None if not in the package.

    ``from pkg import sub`` that resolved to the submodule ``pkg.sub`` points
    to it; anything else points to the module the statement loads.
    """
    if binding.attribute is not None and resolved is not None and resolved.is_module:
        return resolved
    return NodeKey(binding.depends_on) if binding.depends_on in module_names else None


def _resolve(
    module: str,
    attribute: str | None,
    bindings_by_scope: Mapping[NodeKey, list[_Binding]],
    node_keys: set[NodeKey],
    module_names: set[str],
    depth: int,
) -> NodeKey | None:
    """Return the node ``from module import attribute`` refers to.

    Follows Python's order: a definition in the module, then a name the
    module imports itself (a re-export, also through star imports, resolved
    recursively), then the submodule ``module.attribute``. None when the
    name cannot be found in the package.
    """
    if attribute is None:
        return NodeKey(module) if module in module_names else None
    if module not in module_names:
        return None
    definition = NodeKey(module, attribute)
    if definition in node_keys:
        return definition
    if depth < _MAX_REEXPORT_DEPTH:
        for binding in bindings_by_scope.get(NodeKey(module), []):
            # "from . import sub" inside pkg/__init__.py asks the same
            # question again; skip it and fall through to the submodule.
            refers_to_itself = binding.module == module and binding.attribute == attribute
            if binding.local_name == attribute and not refers_to_itself:
                return _resolve(
                    binding.module, binding.attribute, bindings_by_scope, node_keys, module_names, depth + 1
                )
        for binding in bindings_by_scope.get(NodeKey(module), []):
            if binding.is_star_import:
                found = _resolve(binding.module, attribute, bindings_by_scope, node_keys, module_names, depth + 1)
                if found is not None:
                    return found
    submodule = f"{module}.{attribute}"
    return NodeKey(submodule) if submodule in module_names else None
