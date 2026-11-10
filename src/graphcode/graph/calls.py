"""Extract call edges with ``ast``.

One ``CALLS`` edge per (calling scope, called node), with the lines of every
call. The caller is the innermost ``def`` or ``class`` around the call, or the
module for module-level code; calls inside lambdas and comprehensions count
for the enclosing ``def``. Decorators and default values give no edge: they
run once, when the ``def`` statement runs.

A call is resolved with these rules (names are looked up with
:class:`graphcode.graph.names.PackageNames`):

1. a bare name, looked up like Python does: the scope's own nested
   definitions and imports, then enclosing functions, then the module (a
   class body is not visible from its methods; a local variable hides
   outer names);
2. a dotted reference: ``module.func``, ``pkg.sub.func``, ``Class.method``;
3. ``self.method()`` and ``super().method()``: the method on the enclosing
   class or its in-package bases, depth first, left to right; also from a
   closure inside the method, and ``cls(...)`` in a class method;
4. a call to a class points to its ``__init__`` when the class or an
   in-package base defines one, else to the class itself (e.g. a dataclass);
5. type tracking, tagged ``inferred``: ``obj.method()`` where the class of
   ``obj`` is known from a parameter annotation, from an assignment of a
   constructor call or of a call with an annotated return type (``-> Self``
   included), or from a ``self.attr`` typed in the class body, in a method
   or by a ``@property`` return annotation. Variables of enclosing
   functions and of the module are seen from inner scopes.

Rules 1-4 give ``exact`` edges. Everything else is dropped: the standard
library and third-party packages, calls through unannotated parameters,
star re-exports used as ``pkg.name``. Polymorphic dispatch is not guessed:
``self.sample()`` in a base class points to the base's method only. Type
tracking ignores control flow: the last assignment to a name wins; names
assigned in a class body are not tracked.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from graphcode.graph.definitions import SourceReader
from graphcode.graph.imports import ImportAnalysis
from graphcode.graph.model import Confidence, Edge, EdgeKind, Node, NodeKind
from graphcode.graph.names import (
    FUNCTION_KINDS,
    FunctionStatement,
    PackageNames,
    is_name,
    last_name,
    local_variable_names,
    nodes_of_scope,
)
from graphcode.keys import NodeKey

_PROPERTY_DECORATORS = frozenset({"property", "cached_property"})


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


class _Callee(NamedTuple):
    """A resolved call target, before rule 4 maps classes to ``__init__``.

    Attributes
    ----------
    target : NodeKey
        The called node.
    is_exact : bool
        False when type tracking was needed (rule 5).
    receiver : NodeKey or None
        Class of the object (or the class) the method was looked up on, for
        ``obj.method()``; what ``-> Self`` returns.
    """

    target: NodeKey
    is_exact: bool
    receiver: NodeKey | None = None


@dataclass
class _ScopeContext:
    """What is known inside one scope while its calls are resolved.

    Attributes
    ----------
    chain : tuple of NodeKey
        Scopes searched for a bare name, innermost first.
    enclosing : _ScopeContext or None
        Context of the scope Python searches next (a function or the
        module, never a class body); None for a module.
    local_names : frozenset of str
        Names assigned in this scope; they hide the enclosing scopes' names.
    self_name, self_class : str or None, NodeKey or None
        First parameter of a method and the class it belongs to.
    self_is_class : bool
        True in a class method, where ``self_name`` (``cls``) is the class.
    variable_types : dict
        Local name -> class of the object it holds (rule 5).
    """

    chain: tuple[NodeKey, ...]
    enclosing: _ScopeContext | None = None
    local_names: frozenset[str] = frozenset()
    self_name: str | None = None
    self_class: NodeKey | None = None
    self_is_class: bool = False
    variable_types: dict[str, NodeKey] = field(default_factory=dict)


class _CallResolver:
    """Call resolution rules on top of a package's symbol table.

    The caches are filled lazily during one :func:`extract_calls` run and
    discarded with the resolver.
    """

    def __init__(self, names: PackageNames) -> None:
        self._names = names
        self._attribute_types_cache: dict[NodeKey, dict[str, NodeKey]] = {}
        # Context of each scope's last definition, used as the enclosing
        # context of the scopes nested inside it.
        self._context_cache: dict[NodeKey, _ScopeContext] = {}

    def scopes(self) -> Iterator[tuple[NodeKey, list[ast.stmt], _ScopeContext]]:
        """Yield ``(scope key, body, context)`` for every module, class and function."""
        for module_key, body in sorted(self._names.module_bodies.items()):
            yield module_key, body, self._context_of(module_key)
        for class_key, class_statements in sorted(self._names.classes.items()):
            for class_statement in class_statements:
                yield class_key, class_statement.body, self._context_of(class_key)
        for function_key, function_statements in sorted(self._names.functions.items()):
            for function_statement in function_statements:
                yield function_key, function_statement.body, self._function_context(function_key, function_statement)

    def resolve_call(self, call: ast.Call, context: _ScopeContext) -> tuple[NodeKey, Confidence] | None:
        """Return the node a call runs and how sure we are, or None (rule 4 here)."""
        callee = self._callee(call.func, context)
        if callee is None:
            return None
        target, is_exact, _ = callee
        if self._names.kind(target) in (NodeKind.MODULE, NodeKind.PACKAGE):
            return None
        if self._names.kind(target) == NodeKind.CLASS:
            target = self._names.find_member(target, "__init__") or target
        return target, Confidence.EXACT if is_exact else Confidence.INFERRED

    # --- contexts ------------------------------------------------------------

    def _context_of(self, key: NodeKey) -> _ScopeContext:
        """Return the (cached) context of a module, class or function key."""
        if key not in self._context_cache:
            if key.is_module:
                body = self._names.module_bodies[key]
                context = _ScopeContext(chain=(key,), local_names=_assigned_names(body))
                self._track_assignments(body, context)
            elif self._names.kind(key) == NodeKind.CLASS:
                chain = self._names.chain(key)
                context = _ScopeContext(chain=chain, enclosing=self._context_of(chain[1]))
            else:
                context = self._function_context(key, self._names.functions[key][-1])
            self._context_cache[key] = context
        return self._context_cache[key]

    def _function_context(self, key: NodeKey, function: FunctionStatement) -> _ScopeContext:
        """Build the context of one function: ``self`` and local variable types."""
        chain = self._names.chain(key)
        context = _ScopeContext(
            chain=chain, enclosing=self._context_of(chain[1]), local_names=local_variable_names([function])
        )
        if self._names.kind(key) == NodeKind.METHOD:
            context.self_name = _self_parameter(function)
            context.self_class = key.parent_key() if context.self_name else None
            context.self_is_class = any(last_name(item) == "classmethod" for item in function.decorator_list)
        context.variable_types.update(self._parameter_types(function, self._annotation_chain(key)))
        self._track_assignments(function.body, context)
        return context

    def _track_assignments(self, body: list[ast.stmt], context: _ScopeContext) -> None:
        """Record the class of every simply assigned name, in source order."""
        for expression in nodes_of_scope(body):
            if isinstance(expression, ast.AnnAssign) and isinstance(expression.target, ast.Name):
                found = self._names.class_of_annotation(expression.annotation, context.chain)
                _set_or_forget(context.variable_types, expression.target.id, found)
            elif isinstance(expression, ast.Assign) and len(expression.targets) == 1:
                target = expression.targets[0]
                if isinstance(target, ast.Name):
                    _set_or_forget(context.variable_types, target.id, self._type_of(expression.value, context))

    def _annotation_chain(self, key: NodeKey) -> tuple[NodeKey, ...]:
        """Return the scopes a definition's annotations are evaluated in.

        Python evaluates them where the ``def`` statement runs: the class
        body for a method (so a nested class name works), the outer function
        for a nested function, else the module. Not the function itself, so
        a parameter ``graph`` does not hide a module ``graph``.
        """
        return self._names.chain(key.parent_key())  # type: ignore[arg-type]  # a definition has a parent

    def _self_of(self, name: str, context: _ScopeContext) -> _ScopeContext | None:
        """Return the method context whose ``self`` (or ``cls``) is ``name``, closures included."""
        scope: _ScopeContext | None = context
        while scope is not None:
            if name == scope.self_name:
                return scope
            if name in scope.local_names:
                return None
            scope = scope.enclosing
        return None

    # --- call targets ----------------------------------------------------------

    def _callee(self, function: ast.expr, context: _ScopeContext) -> _Callee | None:
        """Return what a call's function expression refers to, or None.

        Each sub-expression is typed at most once here, and the receiver is
        returned rather than recomputed: re-typing it for ``-> Self`` made
        fluent chains (``q.a().b().c()``) cost exponential time.
        """
        if isinstance(function, ast.Name):
            method = self._self_of(function.id, context)
            if method is not None and method.self_is_class:
                return _Callee(method.self_class, True)  # type: ignore[arg-type]  # set with self_name
            found = self._names.lookup_name(function.id, context.chain)
            return _Callee(found, True) if found is not None else None
        if not isinstance(function, ast.Attribute):
            return None
        if context.self_class is not None and _is_super_call(function.value):
            for base in self._names.bases(context.self_class):
                found = self._names.find_member(base, function.attr)
                if found is not None:
                    return _Callee(found, True, context.self_class)
            return None
        if isinstance(function.value, ast.Name):
            method = self._self_of(function.value.id, context)
            if method is not None:
                found = self._names.find_member(method.self_class, function.attr)  # type: ignore[arg-type]
                return _Callee(found, True, method.self_class) if found is not None else None
        reference = self._names.resolve_reference(function, context.chain)
        if reference is not None:
            # Sub.create(): the receiver is the class named before the dot.
            owner_reference = self._names.resolve_reference(function.value, context.chain)
            is_class = owner_reference is not None and self._names.kind(owner_reference) == NodeKind.CLASS
            return _Callee(reference, True, owner_reference if is_class else None)
        owner = self._type_of(function.value, context)
        found = self._names.find_member(owner, function.attr) if owner is not None else None
        return _Callee(found, False, owner) if found is not None else None

    # --- type tracking (rule 5) ----------------------------------------------

    def _type_of(self, expression: ast.expr, context: _ScopeContext) -> NodeKey | None:
        """Return the in-package class of the object an expression evaluates to."""
        if isinstance(expression, ast.Name):
            return self._variable_type(expression.id, context)
        if isinstance(expression, ast.Attribute):
            owner = self._type_of(expression.value, context)
            return self._attribute_types(owner).get(expression.attr) if owner is not None else None
        if not isinstance(expression, ast.Call):
            return None
        callee = self._callee(expression.func, context)
        if callee is None:
            return None
        called = callee.target
        if self._names.kind(called) == NodeKind.CLASS:
            return called
        if self._names.kind(called) not in FUNCTION_KINDS:
            return None
        returns = self._names.functions[called][-1].returns
        if returns is None:
            return None
        if _is_self_annotation(returns):
            # "-> Self" is the receiver's class, else the defining class.
            if callee.receiver is not None:
                return callee.receiver
            defining = called.parent_key()
            return defining if defining is not None and self._names.kind(defining) == NodeKind.CLASS else None
        return self._names.class_of_annotation(returns, self._annotation_chain(called))

    def _variable_type(self, name: str, context: _ScopeContext) -> NodeKey | None:
        """Return the class held by a variable, searching enclosing scopes like Python."""
        scope: _ScopeContext | None = context
        while scope is not None:
            if name == scope.self_name:
                return scope.self_class
            if name in scope.variable_types:
                return scope.variable_types[name]
            if name in scope.local_names:
                return None  # a local of unknown type hides the outer names
            scope = scope.enclosing
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
        """Return attribute types one method defines, without full type tracking.

        A ``@property`` gives its return annotation. Otherwise only
        annotations, annotated parameters (``self.dag = dag``) and
        constructor calls (``self.dag = DAG()``) are used, so that typing an
        attribute never needs the types of other attributes.
        """
        annotation_chain = self._annotation_chain(method_key)
        if method.returns is not None and any(
            last_name(item) in _PROPERTY_DECORATORS for item in method.decorator_list
        ):
            found = self._names.class_of_annotation(method.returns, annotation_chain)
            return {method.name: found} if found is not None else {}
        self_name = _self_parameter(method)
        if self_name is None:
            return {}
        chain = self._names.chain(method_key)
        parameter_types = self._parameter_types(method, annotation_chain)
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


def _assigned_names(body: list[ast.stmt]) -> frozenset[str]:
    """Return the names assigned directly in a module body."""
    return frozenset(
        node.id for node in nodes_of_scope(body) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )


def _set_or_forget(types: dict[str, NodeKey], name: str, found: NodeKey | None) -> None:
    """Record a name's class, or forget it when reassigned to something unknown."""
    if found is None:
        types.pop(name, None)
    else:
        types[name] = found


def _is_self_annotation(annotation: ast.expr) -> bool:
    """Return True for ``Self``, ``typing.Self``, ``"Self"``, ``Self | None``, ``Optional[Self]``."""
    if isinstance(annotation, ast.Constant):
        return annotation.value == "Self"
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_self_annotation(annotation.left) or _is_self_annotation(annotation.right)
    if isinstance(annotation, ast.Subscript) and last_name(annotation.value) == "Optional":
        return _is_self_annotation(annotation.slice)
    return last_name(annotation) == "Self"


def _is_self_attribute(target: ast.expr, self_name: str) -> bool:
    """Return True for ``self.attr`` (with the method's own name for ``self``)."""
    return isinstance(target, ast.Attribute) and is_name(target.value, self_name)


def _is_super_call(expression: ast.expr) -> bool:
    """Return True for ``super()`` and ``super(Class, self)``."""
    return isinstance(expression, ast.Call) and is_name(expression.func, "super")
