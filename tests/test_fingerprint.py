"""Tests for structural fingerprints: what counts as a change, and what does not."""

from __future__ import annotations

import pytest

from graphcode.graph.definitions import extract_definitions


def fingerprints(source: str) -> dict[str, str]:
    """Fingerprint of every node of a one-module package ``pkg``, by short key."""
    sources = {"pkg/__init__.py": "", "pkg/m.py": source}
    nodes = extract_definitions(list(sources), sources.__getitem__)
    return {str(node.key).removeprefix("pkg.m:"): node.fingerprint for node in nodes if str(node.key).startswith("pkg.m")}


BASE = '''
"""Module docstring."""
import math
LIMIT = 3

class Shape(Base):
    """A shape."""
    sides = 0

    def area(self, r):
        """Area."""
        return math.pi * r ** 2

def helper(x):
    def inner(y):
        return y + 1
    return inner(x)
'''


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        ("formatting and comments", BASE.replace("r ** 2", "r**2  # squared").replace("LIMIT = 3", "LIMIT = (3)")),
        ("docstrings", BASE.replace('"""Area."""', '"""Area of a circle."""').replace('"""A shape."""', '"""Shapes."""')),
        ("blank lines moving definitions", BASE.replace("class Shape", "\n\n\nclass Shape")),
    ],
)
def test_no_structural_change(label: str, changed: str) -> None:
    """Formatting, comments, docstrings and line numbers leave every fingerprint unchanged."""
    assert fingerprints(changed) == fingerprints(BASE), label


def test_a_change_marks_only_its_own_definition() -> None:
    """A method body change marks the method, not its class or module; an inner function, not its outer one."""
    before = fingerprints(BASE)
    after = fingerprints(BASE.replace("math.pi * r ** 2", "3.14 * r * r").replace("y + 1", "y + 2"))
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"Shape.area", "helper.<locals>.inner"}


def test_class_changes_are_its_own_lines() -> None:
    """Bases and class attributes change the class, not its methods."""
    before = fingerprints(BASE)
    after = fingerprints(BASE.replace("class Shape(Base)", "class Shape(Base, Mixin)").replace("sides = 0", "sides = 4"))
    assert {key for key in before if before[key] != after[key]} == {"Shape"}


def test_module_changes_are_its_top_level_code() -> None:
    """An import or a constant changes the module node only."""
    before = fingerprints(BASE)
    after = fingerprints(BASE.replace("LIMIT = 3", "LIMIT = 4"))
    assert {key for key in before if before[key] != after[key]} == {"pkg.m"}


def test_rename_keeps_the_fingerprint() -> None:
    """The definition's own name is left out, so a renamed function can be matched as moved."""
    renamed = fingerprints(BASE.replace("def helper(x)", "def assist(x)"))
    assert renamed["assist"] == fingerprints(BASE)["helper"]


def test_duplicate_definitions_are_combined() -> None:
    """A property getter and setter make one node; changing either changes its fingerprint."""
    source = "class C:\n    @property\n    def v(self):\n        return 1\n    @v.setter\n    def v(self, x):\n        pass\n"
    assert fingerprints(source)["C.v"] != fingerprints(source.replace("pass", "self.x = x"))["C.v"]
