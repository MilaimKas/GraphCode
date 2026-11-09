"""Extract call edges with ``ast``.

One ``CALLS`` edge per (calling scope, called node), with the lines of every
call. The caller is the innermost ``def`` or ``class`` around the call, or the
module for module-level code; calls inside lambdas and comprehensions count
for the enclosing ``def``. Decorators give no edge: they run once, at import.

A call is resolved with these rules (names are looked up with
:class:`graphcode.graph.names.PackageNames`):

1. a bare name, looked up like Python does: the scope's own nested
   definitions and imports, then enclosing functions, then the module (a
   class body is not visible from its methods; a local variable hides
   outer names);
2. a dotted reference: ``module.func``, ``pkg.sub.func``, ``Class.method``;
3. ``self.method()`` and ``super().method()``: the method on the enclosing
   class or its in-package bases, depth first, left to right;
4. a call to a class points to its ``__init__`` when the class or an
   in-package base defines one, else to the class itself (e.g. a dataclass);
5. type tracking, tagged ``inferred``: ``obj.method()`` where the class of
   ``obj`` is known from a parameter annotation, from an assignment of a
   constructor call or of a call with an annotated return type, or from a
   ``self.attr`` typed in the class body or in a method.

Rules 1-4 give ``exact`` edges. Everything else is dropped: the standard
library and third-party packages, calls through unannotated parameters,
star re-exports used as ``pkg.name``. Polymorphic dispatch is not guessed:
``self.sample()`` in a base class points to the base's method only. Type
tracking ignores control flow: the last assignment to a name wins.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from graphcode.graph.definitions import SourceReader
from graphcode.graph.imports import ImportAnalysis
from graphcode.graph.model import Confidence, Edge, EdgeKind, Node, NodeKind
from graphcode.graph.names import (
    FUNCTION_KINDS,
    FunctionStatement,
    PackageNames,
    is_name,
    last_name,
    nodes_of_scope,
)
from graphcode.keys import NodeKey


def extract_calls(nodes: Sequence[Node], read_source: SourceReader, imports: ImportAnalysis) -> tuple[Edge, ...]:
    """Return the call edges of the extracted package.

    Parameters
    ----------
    nodes : sequence of Node
        Nodes from :func:`graphcode.graph.definitions.extract_definitions`.
    read_source : SourceReader
        Returns a file's text from its project-relative path.
    imports : ImportAnalysis
        Name tables from :func:`graphcode.graph.imports.extract_imports`.

    Returns
    -------
    tuple of Edge
        ``CALLS`` edges sorted by (source, target); ``EXACT`` if at least
        one call site is exact, else ``INFERRED``.
    """
    resolver = _CallResolver(PackageNames(nodes, read_source, imports))
    lines_by_pair: dict[tuple[NodeKey, NodeKey], list[int]] = {}
    exact_pairs: set[tuple[NodeKey, NodeKey]] = set()
    for scope_key, body, context in resolver.scopes():
        for expression in nodes_of_scope(body):
            if not isinstance(expression, ast.Call):
                continue
            resolved = resolver.resolve_call(expression, context)
            if resolved is None:
                continue
            target, confidence = resolved
            lines_by_pair.setdefault((scope_key, target), []).append(expression.lineno)
            if confidence == Confidence.EXACT:
                exact_pairs.add((scope_key, target))
    return tuple(
        Edge(
            source,
            target,
            EdgeKind.CALLS,
            Confidence.EXACT if (source, target) in exact_pairs else Confidence.INFERRED,
            tuple(lines),
        )
        for (source, target), lines in sorted(lines_by_pair.items())
    )


@dataclass
class _ScopeContext:
    """What is known inside one scope while its calls are resolved.

    Attributes
    ----------
    chain : tuple of NodeKey
        Scopes searched for a bare name, innermost first.
    self_name, self_class : str or None, NodeKey or None
        First parameter of a method and the class it belongs to.
    variable_types : dict
        Local name -> class of the object it holds (rule 5).
    """

    chain: tuple[NodeKey, ...]
    self_name: str | None = None
    self_class: NodeKey | None = None
    variable_types: dict[str, NodeKey] = field(default_factory=dict)


class _CallResolver:
    """Call resolution rules on top of a package's symbol table.

    The attribute-type cache is filled lazily during one
    :func:`extract_calls` run and discarded with the resolver.
    """

    def __init__(self, names: PackageNames) -> None:
        self._names = names
        self._attribute_types_cache: dict[NodeKey, dict[str, NodeKey]] = {}

    def scopes(self) -> Iterator[tuple[NodeKey, list[ast.stmt], _ScopeContext]]:
        """Yield ``(scope key, body, context)`` for every module, class and function."""
        for module_key, body in sorted(self._names.module_bodies.items()):
            yield module_key, body, _ScopeContext(chain=(module_key,))
        for class_key, class_statements in sorted(self._names.classes.items()):
            for class_statement in class_statements:
                yield class_key, class_statement.body, _ScopeContext(chain=self._names.chain(class_key))
        for function_key, function_statements in sorted(self._names.functions.items()):
            for function_statement in function_statements:
                yield function_key, function_statement.body, self._context_for(function_key, function_statement)

    def resolve_call(self, call: ast.Call, context: _ScopeContext) -> tuple[NodeKey, Confidence] | None:
        """Return the node a call runs and how sure we are, or None (rule 4 here)."""
        callee = self._callee(call.func, context)
        if callee is None:
            return None
        target, is_exact = callee
        if self._names.kind(target) in (NodeKind.MODULE, NodeKind.PACKAGE):
            return None
        if self._names.kind(target) == NodeKind.CLASS:
            target = self._names.find_member(target, "__init__") or target
        return target, Confidence.EXACT if is_exact else Confidence.INFERRED

    def _context_for(self, key: NodeKey, function: FunctionStatement) -> _ScopeContext:
        """Build the context of one function: ``self`` and local variable types."""
        chain = self._names.chain(key)
        context = _ScopeContext(chain=chain)
        if self._names.kind(key) == NodeKind.METHOD:
            context.self_name = _self_parameter(function)
            context.self_class = key.parent_key() if context.self_name else None
        context.variable_types.update(self._parameter_types(function, chain))
        for expression in nodes_of_scope(function.body):
            if isinstance(expression, ast.AnnAssign) and isinstance(expression.target, ast.Name):
                found = self._names.class_of_annotation(expression.annotation, chain)
                _set_or_forget(context.variable_types, expression.target.id, found)
            elif isinstance(expression, ast.Assign) and len(expression.targets) == 1:
                target = expression.targets[0]
                if isinstance(target, ast.Name):
                    _set_or_forget(context.variable_types, target.id, self._type_of(expression.value, context))
        return context

    def _callee(self, function: ast.expr, context: _ScopeContext) -> tuple[NodeKey, bool] | None:
        """Return ``(called node, is exact)`` before rule 4 maps classes to ``__init__``."""
        if isinstance(function, ast.Name):
            found = self._names.lookup_name(function.id, context.chain)
            return (found, True) if found is not None else None
        if not isinstance(function, ast.Attribute):
            return None
        if context.self_class is not None and _is_super_call(function.value):
            for base in self._names.bases(context.self_class):
                found = self._names.find_member(base, function.attr)
                if found is not None:
                    return found, True
            return None
        if context.self_class is not None and is_name(function.value, context.self_name):
            found = self._names.find_member(context.self_class, function.attr)
            return (found, True) if found is not None else None
        reference = self._names.resolve_reference(function, context.chain)
        if reference is not None:
            return reference, True
        owner = self._type_of(function.value, context)
        found = self._names.find_member(owner, function.attr) if owner is not None else None
        return (found, False) if found is not None else None

    # --- type tracking (rule 5) ----------------------------------------------

    def _type_of(self, expression: ast.expr, context: _ScopeContext) -> NodeKey | None:
        """Return the in-package class of the object an expression evaluates to."""
        if isinstance(expression, ast.Name):
            if expression.id == context.self_name:
                return context.self_class
            return context.variable_types.get(expression.id)
        if isinstance(expression, ast.Attribute):
            owner = self._type_of(expression.value, context)
            return self._attribute_types(owner).get(expression.attr) if owner is not None else None
        if not isinstance(expression, ast.Call):
            return None
        callee = self._callee(expression.func, context)
        if callee is None:
            return None
        called = callee[0]
        if self._names.kind(called) == NodeKind.CLASS:
            return called
        if self._names.kind(called) in FUNCTION_KINDS:
            returns = self._names.functions[called][-1].returns
            return self._names.class_of_annotation(returns, self._names.chain(called)) if returns else None
        return None

    def _attribute_types(self, class_key: NodeKey) -> dict[str, NodeKey]:
        """Return attribute name -> class, from the class body, its methods and its bases."""
        if class_key in self._attribute_types_cache:
            return self._attribute_types_cache[class_key]
        types: dict[str, NodeKey] = {}
        self._attribute_types_cache[class_key] = types  # stored first: stops inheritance loops
        for base in reversed(self._names.bases(class_key)):
            types.update(self._attribute_types(base))
        chain = self._names.chain(class_key)
        for class_statement in self._names.classes[class_key]:
            for item in class_statement.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    found = self._names.class_of_annotation(item.annotation, chain)
                    _set_or_forget(types, item.target.id, found)
        for method_key in self._names.children(class_key):
            if self._names.kind(method_key) == NodeKind.METHOD:
                for method in self._names.functions[method_key]:
                    types.update(self._self_attribute_types(method_key, method))
        return types

    def _self_attribute_types(self, method_key: NodeKey, method: FunctionStatement) -> dict[str, NodeKey]:
        """Return ``self.attr`` types set in one method, without full type tracking.

        Only annotations, annotated parameters (``self.dag = dag``) and
        constructor calls (``self.dag = DAG()``) are used, so that typing an
        attribute never needs the types of other attributes.
        """
        self_name = _self_parameter(method)
        if self_name is None:
            return {}
        chain = self._names.chain(method_key)
        parameter_types = self._parameter_types(method, chain)
        types: dict[str, NodeKey] = {}
        for expression in nodes_of_scope(method.body):
            if isinstance(expression, ast.AnnAssign) and _is_self_attribute(expression.target, self_name):
                found = self._names.class_of_annotation(expression.annotation, chain)
                _set_or_forget(types, expression.target.attr, found)  # type: ignore[union-attr]
            elif isinstance(expression, ast.Assign):
                for target in expression.targets:
                    if _is_self_attribute(target, self_name):
                        found = self._shallow_type(expression.value, parameter_types, chain)
                        _set_or_forget(types, target.attr, found)  # type: ignore[attr-defined]
        return types

    def _shallow_type(
        self, value: ast.expr, parameter_types: dict[str, NodeKey], chain: tuple[NodeKey, ...]
    ) -> NodeKey | None:
        """Return the class of an annotated parameter or of a constructor call."""
        if isinstance(value, ast.Name):
            return parameter_types.get(value.id)
        if isinstance(value, ast.Call):
            called = self._names.resolve_reference(value.func, chain)
            return called if called is not None and self._names.kind(called) == NodeKind.CLASS else None
        return None

    def _parameter_types(self, function: FunctionStatement, chain: tuple[NodeKey, ...]) -> dict[str, NodeKey]:
        """Return parameter name -> class, from the annotations (``*args``/``**kwargs`` excluded)."""
        arguments = function.args
        types: dict[str, NodeKey] = {}
        for parameter in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
            if parameter.annotation is not None:
                found = self._names.class_of_annotation(parameter.annotation, chain)
                if found is not None:
                    types[parameter.arg] = found
        return types


def _self_parameter(method: FunctionStatement) -> str | None:
    """Return the name of a method's first parameter, None for a static method."""
    if any(last_name(decorator) == "staticmethod" for decorator in method.decorator_list):
        return None
    positional = [*method.args.posonlyargs, *method.args.args]
    return positional[0].arg if positional else None


def _set_or_forget(types: dict[str, NodeKey], name: str, found: NodeKey | None) -> None:
    """Record a name's class, or forget it when reassigned to something unknown."""
    if found is None:
        types.pop(name, None)
    else:
        types[name] = found


def _is_self_attribute(target: ast.expr, self_name: str) -> bool:
    """Return True for ``self.attr`` (with the method's own name for ``self``)."""
    return isinstance(target, ast.Attribute) and is_name(target.value, self_name)


def _is_super_call(expression: ast.expr) -> bool:
    """Return True for ``super()`` and ``super(Class, self)``."""
    return isinstance(expression, ast.Call) and is_name(expression.func, "super")
