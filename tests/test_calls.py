"""Tests for :mod:`graphcode.graph.calls` on the sample fixture and edge cases."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from graphcode.graph.calls import extract_calls
from graphcode.graph.definitions import extract_definitions
from graphcode.graph.imports import extract_imports
from graphcode.graph.model import (
    ArchitectureGraph,
    Confidence,
    Edge,
    EdgeKind,
    GraphHeader,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"

EXACT = Confidence.EXACT.value
INFERRED = Confidence.INFERRED.value


def analyse(file_paths: list[str], read_source: Callable[[str], str]) -> tuple[Edge, ...]:
    """Run definitions, imports and calls, and check the result is a valid graph."""
    nodes = extract_definitions(file_paths, read_source)
    imports = extract_imports(nodes, read_source)
    calls = extract_calls(nodes, read_source, imports)
    ArchitectureGraph(GraphHeader.for_current_build(None, False), nodes, imports.edges + calls)
    return calls


def call_table(edges: tuple[Edge, ...]) -> dict[tuple[str, str], tuple[str, tuple[int, ...]]]:
    """Return ``(source, target) -> (confidence, lines)`` with keys as text."""
    assert all(edge.kind == EdgeKind.CALLS for edge in edges)
    return {(str(edge.source), str(edge.target)): (edge.confidence.value, edge.lines) for edge in edges}


def calls_of(sources: dict[str, str]) -> dict[tuple[str, str], tuple[str, tuple[int, ...]]]:
    """Analyse in-memory sources and return their call table."""
    return call_table(analyse(list(sources), sources.__getitem__))


def pairs_of(sources: dict[str, str]) -> set[tuple[str, str]]:
    """Return only the ``(source, target)`` pairs of in-memory sources."""
    return set(calls_of(sources))


# --- golden answer on the fixture --------------------------------------------

B = "sample.builders"
C = "sample.core"

EXPECTED_FIXTURE_CALLS = {
    (f"{B}:build_default", f"{C}:Graph.__init__"): (EXACT, (19,)),
    (f"{B}:build_default", f"{C}:Graph.add_node"): (INFERRED, (20, 21)),
    (f"{B}:build_default", f"{C}:Graph.add_edge"): (INFERRED, (22,)),
    (f"{B}:build_chain", f"{C}:Graph.__init__"): (EXACT, (33,)),
    (f"{B}:build_chain", f"{C}:Graph.add_node"): (INFERRED, (35,)),
    (f"{B}:build_chain", f"{B}:build_chain.<locals>.link"): (EXACT, (36,)),
    (f"{B}:build_chain.<locals>.link", f"{C}:Graph.add_edge"): (INFERRED, (31,)),
    (f"{B}:build_noisy", f"{C}:Graph.__init__"): (EXACT, (48,)),
    (f"{B}:build_noisy", f"{B}:build_noisy.<locals>.Labeller"): (EXACT, (49,)),
    (f"{B}:build_noisy", f"{B}:build_noisy.<locals>.Labeller.label"): (INFERRED, (49,)),
    (f"{B}:build_noisy", f"{C}:Graph.add_node"): (INFERRED, (49,)),
    (f"{B}:build_noisy", "sample.noise.gaussian:GaussianNoise.__init__"): (EXACT, (50,)),
    (f"{C}:Graph.add_node", f"{C}:GraphError"): (EXACT, (40,)),
    (f"{C}:Graph.add_node", f"{C}:Node"): (EXACT, (41,)),
    (f"{C}:Graph.add_edge", f"{C}:Graph._check_exists"): (EXACT, (47, 48)),
    (f"{C}:Graph._check_exists", f"{C}:GraphError"): (EXACT, (53,)),
    (f"{C}:Graph.get", f"{C}:GraphError"): (EXACT, (77,)),
    ("sample.utils:timed.<locals>.wrapper", "sample.utils:_elapsed_since"): (EXACT, (22,)),
    ("sample.noise.base:Noise.sample_many", "sample.noise.base:Noise.sample"): (EXACT, (24,)),
    ("sample.noise.gaussian:GaussianNoise.__init__", "sample.noise.base:Noise.__init__"): (EXACT, (17,)),
    ("sample.noise.gaussian:GaussianNoise.mean_of", "sample.noise.base:Noise.sample_many"): (EXACT, (26,)),
}


@pytest.fixture(scope="module")
def fixture_calls() -> tuple[Edge, ...]:
    """Analyse the fixture package once."""
    paths = [path.relative_to(FIXTURE_ROOT).as_posix() for path in (FIXTURE_ROOT / "sample").rglob("*.py")]
    return analyse(paths, lambda path: (FIXTURE_ROOT / path).read_text(encoding="utf-8"))


def test_fixture_call_edges(fixture_calls: tuple[Edge, ...]) -> None:
    """The 21 hand-checked edges: 15 exact, 6 inferred, no decorator edge."""
    assert call_table(fixture_calls) == EXPECTED_FIXTURE_CALLS


# --- edge cases on synthetic sources -----------------------------------------


def test_module_attribute_calls() -> None:
    """``import pkg.mod``, ``from pkg import mod`` and aliases resolve ``mod.f()``."""
    source = (
        "import pkg.mod\nfrom pkg import mod\nimport pkg.mod as alias\n"
        "def a(): pkg.mod.f()\ndef b(): mod.f()\ndef c(): alias.f()\n"
    )
    pairs = pairs_of({"pkg/__init__.py": "", "pkg/mod.py": "def f(): ...\n", "pkg/user.py": source})
    assert {("pkg.user:a", "pkg.mod:f"), ("pkg.user:b", "pkg.mod:f"), ("pkg.user:c", "pkg.mod:f")} <= pairs


def test_class_reference_method_call() -> None:
    """``Class.method(obj)`` is an exact call to the method."""
    source = "class A:\n    def m(self): ...\ndef f(a):\n    A.m(a)\n"
    assert calls_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:f", "pkg.m:A.m"): (EXACT, (4,))}


def test_inherited_init_is_the_constructor_target() -> None:
    """A class without ``__init__`` whose base has one points to the base's."""
    source = "class Base:\n    def __init__(self): ...\nclass Child(Base): ...\ndef f():\n    Child()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:f", "pkg.m:Base.__init__")}


def test_return_annotation_types_the_result() -> None:
    """``g = make()`` with ``def make() -> Graph`` types ``g``; also as a string."""
    source = (
        "class Graph:\n    def add(self): ...\n"
        "def make() -> Graph: ...\n"
        "def make_later() -> 'Graph': ...\n"
        "def f():\n    g = make()\n    g.add()\n    make_later().add()\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:f", "pkg.m:Graph.add")] == (INFERRED, (7, 8))


def test_self_attribute_types() -> None:
    """``self.dag`` typed by an annotated parameter, a constructor, or the class body."""
    source = (
        "class Dag:\n    def run(self): ...\n"
        "class Holder:\n"
        "    spare: Dag | None\n"
        "    def __init__(self, dag: Dag):\n        self.dag = dag\n        self.own = Dag()\n"
        "    def go(self):\n        self.dag.run()\n        self.own.run()\n        self.spare.run()\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:Holder.go", "pkg.m:Dag.run")] == (INFERRED, (9, 10, 11))


def test_self_attribute_typed_in_a_base_class() -> None:
    """An attribute set in the base's ``__init__`` is known in the subclass."""
    source = (
        "class Dag:\n    def run(self): ...\n"
        "class Base:\n    def __init__(self):\n        self.dag = Dag()\n"
        "class Child(Base):\n    def go(self):\n        self.dag.run()\n"
    )
    assert ("pkg.m:Child.go", "pkg.m:Dag.run") in pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})


def test_optional_annotation() -> None:
    """``Optional[Graph]`` from typing types a parameter."""
    source = (
        "from typing import Optional\n"
        "class Graph:\n    def add(self): ...\n"
        "def f(g: Optional[Graph]):\n    g.add()\n"
    )
    assert ("pkg.m:f", "pkg.m:Graph.add") in pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})


def test_decorators_give_no_edge() -> None:
    """Neither the module nor the decorated function calls the decorator."""
    source = "def deco(function): return function\n@deco\ndef f(): ...\nclass A:\n    @deco\n    def m(self): ...\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == set()


def test_module_and_class_body_calls() -> None:
    """Module-level code is credited to the module; class-body code to the class."""
    source = "def make(): ...\nVALUE = make()\nclass A:\n    DEFAULT = make()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m", "pkg.m:make"), ("pkg.m:A", "pkg.m:make")}


def test_class_body_is_not_visible_from_methods() -> None:
    """``helper`` inside a method is the module function, not the class attribute."""
    source = "def helper(): ...\nclass A:\n    def helper(self): ...\n    def m(self):\n        helper()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:A.m", "pkg.m:helper")}


def test_local_names_hide_package_names() -> None:
    """A local variable or local external import hides a module-level name."""
    source = (
        "def load(): ...\n"
        "def by_variable(load):\n    load()\n"
        "def by_import():\n    from json import load\n    load()\n"
        "def plain():\n    load()\n"
    )
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:plain", "pkg.m:load")}


def test_static_method_has_no_self() -> None:
    """In a static method, the first parameter is not ``self``."""
    source = (
        "class A:\n    def m(self): ...\n"
        "    @staticmethod\n    def s(other):\n        other.m()\n"
    )
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == set()


def test_super_skips_own_class_and_polymorphism_is_not_guessed() -> None:
    """``super().m()`` goes to the base; ``self.m()`` in the base stays in the base."""
    source = (
        "class Base:\n    def m(self): ...\n    def run(self):\n        self.m()\n"
        "class Child(Base):\n    def m(self):\n        super().m()\n"
    )
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {
        ("pkg.m:Base.run", "pkg.m:Base.m"),
        ("pkg.m:Child.m", "pkg.m:Base.m"),
    }


def test_recursion_is_a_self_edge() -> None:
    """A recursive function calls itself."""
    source = "def f(n):\n    return f(n - 1) if n else 0\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:f", "pkg.m:f")}


def test_reassignment_forgets_the_type() -> None:
    """The last assignment wins: an unknown value clears a known type."""
    source = "class G:\n    def add(self): ...\ndef f(other):\n    g = G()\n    g = other\n    g.add()\n"
    # Only the constructor call remains (G has no __init__, so the class).
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:f", "pkg.m:G")}


def test_exact_wins_over_inferred_on_the_same_edge() -> None:
    """One exact call site makes the whole edge exact."""
    source = "class G:\n    def add(self): ...\ndef f(g: G):\n    g.add()\n    G.add(g)\n"
    assert calls_of({"pkg/__init__.py": "", "pkg/m.py": source})[("pkg.m:f", "pkg.m:G.add")] == (EXACT, (4, 5))
