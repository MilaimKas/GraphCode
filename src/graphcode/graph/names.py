"""Symbol table of the analysed package: which node a name refers to.

:class:`PackageNames` answers the questions every edge resolver asks:

- which scopes Python searches for a bare name (``chain``): the scope, then
  enclosing functions, then the module, skipping enclosing class bodies;
- which node a name or dotted name refers to (``lookup_name``,
  ``resolve_reference``): ``Graph``, ``mod.func``, ``pkg.sub.Class.method``;
- what a class inherits (``bases``, ``find_member``): in-package bases only,
  searched depth first, left to right;
- which in-package class an annotation names (``class_of_annotation``).

It is built once per extraction from the nodes, the parsed sources and the
import name tables, and is not changed afterwards (apart from caches).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Sequence

from graphcode.graph.definitions import (
    DefinitionStatement,
    SourceReader,
    definitions_of_scope,
)
from graphcode.graph.imports import ImportAnalysis
from graphcode.graph.model import Node, NodeKind
from graphcode.keys import NodeKey

FunctionStatement = ast.FunctionDef | ast.AsyncFunctionDef
FUNCTION_KINDS = frozenset({NodeKind.FUNCTION, NodeKind.METHOD})


class PackageNames:
    """Name lookup tables of one package.

    Parameters
    ----------
    nodes : sequence of Node
        Nodes from :func:`graphcode.graph.definitions.extract_definitions`.
    read_source : SourceReader
        Returns a file's text from its project-relative path.
    imports : ImportAnalysis
        Name tables from :func:`graphcode.graph.imports.extract_imports`.

    Attributes
    ----------
    module_bodies : dict
        Module key -> statements of the module.
    classes, functions : dict
        Key -> every ast statement defining it, in source order; several for
        property setters and ``@overload`` stubs. The last one is the
        definition that exists at run time.
    """

    def __init__(self, nodes: Sequence[Node], read_source: SourceReader, imports: ImportAnalysis) -> None:
        self._kinds = {node.key: node.kind for node in nodes}
        self._imports = imports
        self._children: dict[NodeKey, list[NodeKey]] = {}
        for node in nodes:
            if node.parent is not None:
                self._children.setdefault(node.parent, []).append(node.key)
        self.module_bodies: dict[NodeKey, list[ast.stmt]] = {}
        self.classes: dict[NodeKey, list[ast.ClassDef]] = {}
        self.functions: dict[NodeKey, list[FunctionStatement]] = {}
        for node in nodes:
            if node.key.is_module:
                tree = ast.parse(read_source(node.file), filename=node.file)
                self.module_bodies[node.key] = tree.body
                self._index_definitions(tree.body, node.key, inside_function=False)
        self._local_variables = {
            key: local_variable_names(statements)
            for key, statements in self.functions.items()
            if self._kinds.get(key) in FUNCTION_KINDS
        }
        self._bases_cache: dict[NodeKey, tuple[NodeKey, ...]] = {}

    def _index_definitions(self, body: list[ast.stmt], scope_key: NodeKey, inside_function: bool) -> None:
        """Record the ast of every definition, with keys built like definitions.py."""
        for statement in definitions_of_scope(body):
            key = scope_key.child(statement.name, inside_function=inside_function)
            if isinstance(statement, ast.ClassDef):
                self.classes.setdefault(key, []).append(statement)
                self._index_definitions(statement.body, key, inside_function=False)
            else:
                self.functions.setdefault(key, []).append(statement)
                self._index_definitions(statement.body, key, inside_function=True)

    def kind(self, key: NodeKey) -> NodeKind:
        """Return the kind of a node of the package."""
        return self._kinds[key]

    def children(self, key: NodeKey) -> list[NodeKey]:
        """Return the keys of the nodes directly inside ``key``."""
        return self._children.get(key, [])

    # --- scopes and names -----------------------------------------------------

    def chain(self, scope_key: NodeKey) -> tuple[NodeKey, ...]:
        """Return the scope and the enclosing scopes Python searches, up to the module.

        Enclosing classes are skipped: ``x`` in a method never finds a class
        attribute ``x``. A class's own body does see its names.
        """
        chain = [scope_key]
        key = scope_key
        while not key.is_module:
            key = key.parent_key()  # type: ignore[assignment]  # never None below a module
            if self._kinds[key] != NodeKind.CLASS:
                chain.append(key)
        return tuple(chain)

    def lookup_name(self, name: str, chain: tuple[NodeKey, ...]) -> NodeKey | None:
        """Return the node a bare name refers to, or None if it is not in the package.

        In each scope: a nested definition, then an in-package import; an
        external import or a local variable of that name stops the search.
        """
        for scope in chain:
            definition = scope.child(name, inside_function=self._kinds[scope] in FUNCTION_KINDS)
            if definition in self._kinds:
                return definition
            imported = self._imports.names.get(scope, {}).get(name)
            if imported is not None:
                return imported
            if name in self._imports.external_names.get(scope, frozenset()):
                return None
            if name in self._local_variables.get(scope, frozenset()):
                return None
        return None

    def resolve_reference(self, expression: ast.expr, chain: tuple[NodeKey, ...]) -> NodeKey | None:
        """Return the node a name or dotted name (``pkg.mod.func``) refers to."""
        if isinstance(expression, ast.Name):
            return self.lookup_name(expression.id, chain)
        if not isinstance(expression, ast.Attribute):
            return None
        owner = self.resolve_reference(expression.value, chain)
        if owner is None:
            return None
        if owner.is_module:
            return self._member_of_module(owner, expression.attr)
        if self._kinds[owner] == NodeKind.CLASS:
            return self.find_member(owner, expression.attr)
        return None

    def _member_of_module(self, module_key: NodeKey, name: str) -> NodeKey | None:
        """Return ``module.name``: a definition, a re-exported name, or a submodule."""
        definition = NodeKey(module_key.module, name)
        if definition in self._kinds:
            return definition
        reexported = self._imports.names.get(module_key, {}).get(name)
        if reexported is not None:
            return reexported
        submodule = NodeKey(f"{module_key.module}.{name}")
        return submodule if submodule in self._kinds else None

    # --- classes ----------------------------------------------------------------

    def bases(self, class_key: NodeKey) -> tuple[NodeKey, ...]:
        """Return the in-package base classes, in source order."""
        if class_key not in self._bases_cache:
            statement = self.classes[class_key][-1]
            # Bases are evaluated in the scope around the class statement.
            chain = self.chain(class_key.parent_key())  # type: ignore[arg-type]  # a class has a parent
            bases = []
            for base in statement.bases:
                # Generic bases (Base[int]) are looked up by their class.
                found = self.resolve_reference(base.value if isinstance(base, ast.Subscript) else base, chain)
                if found is not None and found != class_key and self._kinds[found] == NodeKind.CLASS:
                    bases.append(found)
            self._bases_cache[class_key] = tuple(bases)
        return self._bases_cache[class_key]

    def find_member(self, class_key: NodeKey, name: str) -> NodeKey | None:
        """Return ``name`` defined on the class or, depth first, on its bases."""
        return self._find_member(class_key, name, visited=set())

    def _find_member(self, class_key: NodeKey, name: str, visited: set[NodeKey]) -> NodeKey | None:
        """Depth-first search for :meth:`find_member`; ``visited`` stops loops."""
        if class_key in visited:
            return None
        visited.add(class_key)
        member = class_key.child(name)
        if member in self._kinds:
            return member
        for base in self.bases(class_key):
            found = self._find_member(base, name, visited)
            if found is not None:
                return found
        return None

    def class_of_annotation(self, annotation: ast.expr, chain: tuple[NodeKey, ...]) -> NodeKey | None:
        """Return the in-package class an annotation names.

        Handles ``Graph``, ``mod.Graph``, ``"Graph"``, ``Graph | None`` and
        ``Optional[Graph]``. Containers (``list[Graph]``) give None.
        """
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            left = self.class_of_annotation(annotation.left, chain)
            return left if left is not None else self.class_of_annotation(annotation.right, chain)
        if isinstance(annotation, ast.Subscript) and last_name(annotation.value) == "Optional":
            return self.class_of_annotation(annotation.slice, chain)
        found = self.resolve_reference(annotation, chain)
        return found if found is not None and self._kinds[found] == NodeKind.CLASS else None


# --- ast helpers ---------------------------------------------------------------


def nodes_of_scope(nodes: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Yield every ast node of a scope in source order, skipping nested definitions.

    Skipping a nested ``def`` or ``class`` entirely also skips its
    decorators and default values.
    """
    for node in nodes:
        if isinstance(node, DefinitionStatement):
            continue
        yield node
        yield from nodes_of_scope(ast.iter_child_nodes(node))


def parameters_of(function: FunctionStatement) -> list[ast.arg]:
    """Return every parameter of a function, ``*args`` and ``**kwargs`` included."""
    arguments = function.args
    parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    return parameters + [parameter for parameter in (arguments.vararg, arguments.kwarg) if parameter is not None]


def local_variable_names(statements: Sequence[FunctionStatement]) -> frozenset[str]:
    """Return the parameters and assigned names of a function (its local variables)."""
    names: set[str] = set()
    for statement in statements:
        names.update(parameter.arg for parameter in parameters_of(statement))
        for node in nodes_of_scope(statement.body):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return frozenset(names)


def last_name(expression: ast.expr) -> str | None:
    """Return ``x`` for ``x`` or ``a.b.x``, else None."""
    if isinstance(expression, ast.Name):
        return expression.id
    return expression.attr if isinstance(expression, ast.Attribute) else None


def is_name(expression: ast.expr, name: str | None) -> bool:
    """Return True if the expression is exactly the bare name ``name``."""
    return name is not None and isinstance(expression, ast.Name) and expression.id == name
