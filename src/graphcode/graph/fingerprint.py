"""Structural fingerprints: has a definition really changed between two versions?

A fingerprint is a short hash of a definition's ``ast`` tree, so that
formatting and comments (not in the tree) never count as a change. Also left
out, so that a change is reported on the definition it belongs to:

- line and column numbers;
- the docstring;
- the definition's own name, so that a renamed function can be recognised
  as moved rather than removed and added;
- nested functions and classes: they are nodes of their own. A method's change
  marks the method, not its class, and a function's change does not mark its
  module.

Examples::

    def area(r):                        def area(r):
        '''Circle area.'''                  # pi r squared
        return 3.14 * r ** 2                return 3.14 * r**2       -> same fingerprint

    def area(r): return 3.14 * r ** 2   def area(radius): ...       -> different
"""

from __future__ import annotations

import ast
import copy
import hashlib
import sys
from collections.abc import Sequence

DefinitionStatement = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef

_FINGERPRINT_BYTES = 8  # 16 hexadecimal characters: ample for one package's definitions
FINGERPRINT_RULE_VERSION = 1  # raise when what goes into a fingerprint changes


def fingerprint_scheme() -> str:
    """Tag of how fingerprints are computed now, e.g. ``"1-py3.12"``.

    Python's ``ast.dump`` text changes between versions (3.12 added type
    parameters, 3.13 stopped printing empty fields), so the same code gets
    different fingerprints under two Python versions. Only fingerprints with
    the same scheme can be compared.
    """
    return f"{FINGERPRINT_RULE_VERSION}-py{sys.version_info.major}.{sys.version_info.minor}"


class _WithoutNestedDefinitions(ast.NodeTransformer):
    """Drop every function and class definition met (also inside ``if`` or ``try`` blocks)."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Remove a nested ``def``."""
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Remove a nested ``async def``."""
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Remove a nested ``class``."""
        return


def _own_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """The statements that belong to a scope itself: no docstring, no nested definitions."""
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        body = body[1:]  # the docstring
    remover = _WithoutNestedDefinitions()
    kept = (remover.visit(statement) for statement in body)
    return [statement for statement in kept if statement is not None]


def _hash(text: str) -> str:
    """Short, stable hash of a text."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=_FINGERPRINT_BYTES).hexdigest()


def _dump(tree: ast.AST) -> str:
    """The tree as text, without line and column numbers."""
    return ast.dump(tree, include_attributes=False)


def definition_fingerprint(statement: DefinitionStatement) -> str:
    """Fingerprint of one ``class`` or ``def`` statement (the original is not modified)."""
    own = copy.deepcopy(statement)
    own.name = ""
    own.body = _own_statements(own.body)
    return _hash(_dump(own))


def module_fingerprint(tree: ast.Module) -> str:
    """Fingerprint of a module's own top-level code (imports, constants, scripts)."""
    own = ast.Module(body=_own_statements(copy.deepcopy(tree.body)), type_ignores=[])
    return _hash(_dump(own))


def combined_fingerprint(parts: Sequence[str]) -> str:
    """One fingerprint for a key defined several times (property getter and setter, ``@overload``)."""
    return parts[0] if len(parts) == 1 else _hash("|".join(parts))
