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


# --- review fixes: enclosing scopes, cls, Self, property, annotation scope -----


def test_module_level_type_tracking() -> None:
    """Module code is typed, and functions see module-level instances."""
    source = (
        "class Registry:\n    def add(self): ...\n"
        "registry = Registry()\nregistry.add()\n"
        "def f():\n    registry.add()\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m", "pkg.m:Registry.add")] == (INFERRED, (4,))
    assert table[("pkg.m:f", "pkg.m:Registry.add")] == (INFERRED, (6,))


def test_closures_see_self_and_outer_variables() -> None:
    """An inner function uses the method's ``self`` and the outer function's variables."""
    source = (
        "class G:\n    def add(self): ...\n"
        "    def m(self):\n        def inner():\n            self.add()\n"
        "def outer(g: G):\n    def inner():\n        g.add()\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:G.m.<locals>.inner", "pkg.m:G.add")] == (EXACT, (5,))
    assert table[("pkg.m:outer.<locals>.inner", "pkg.m:G.add")] == (INFERRED, (8,))


def test_inner_local_hides_outer_variable_type() -> None:
    """A local of unknown type in the inner function hides the outer one."""
    source = "class G:\n    def add(self): ...\ndef outer(g: G):\n    def inner(g):\n        g.add()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == set()


def test_cls_call_in_classmethod() -> None:
    """``cls(...)`` is the constructor; its result is typed."""
    source = (
        "class G:\n    def __init__(self): ...\n    def add(self): ...\n"
        "    @classmethod\n    def make(cls):\n        made = cls()\n        made.add()\n        return made\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:G.make", "pkg.m:G.__init__")] == (EXACT, (6,))
    assert table[("pkg.m:G.make", "pkg.m:G.add")] == (INFERRED, (7,))


def test_self_return_annotation() -> None:
    """``-> Self`` types the result as the receiver's class."""
    source = (
        "from typing import Self\n"
        "class G:\n    def copy(self) -> Self: ...\n    def add(self): ...\n"
        "def f(g: G):\n    g.copy().add()\n"
    )
    assert ("pkg.m:f", "pkg.m:G.add") in pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})


def test_property_return_annotation_types_the_attribute() -> None:
    """``self.graph.add()`` where ``graph`` is a ``@property -> Graph``."""
    source = (
        "class Graph:\n    def add(self): ...\n"
        "class Holder:\n    @property\n    def graph(self) -> Graph: ...\n"
        "    def go(self):\n        self.graph.add()\n"
    )
    assert ("pkg.m:Holder.go", "pkg.m:Graph.add") in pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})


def test_annotations_are_resolved_where_the_def_runs() -> None:
    """A parameter does not hide the module it is annotated with; a method sees class names."""
    sources = {
        "pkg/__init__.py": "",
        "pkg/graph.py": "class Graph:\n    def add(self): ...\n",
        "pkg/m.py": (
            "from pkg import graph\n"
            "def draw(graph: graph.Graph):\n    graph.add()\n"
            "class A:\n    class Config:\n        def load(self): ...\n"
            "    def m(self, config: Config):\n        config.load()\n"
        ),
    }
    pairs = pairs_of(sources)
    assert ("pkg.m:draw", "pkg.graph:Graph.add") in pairs
    assert ("pkg.m:A.m", "pkg.m:A.Config.load") in pairs


# --- review test gaps ------------------------------------------------------------


def test_comprehension_and_lambda_calls_count_for_the_def() -> None:
    """Calls inside a comprehension or a lambda are credited to the enclosing def."""
    source = "def make(x): ...\ndef f(xs):\n    built = [make(x) for x in xs]\n    return lambda: make(1)\n"
    assert calls_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:f", "pkg.m:make"): (EXACT, (3, 4))}


def test_inner_function_calls_a_sibling_from_the_enclosing_function() -> None:
    """``helper`` defined in ``outer`` is found from ``outer.<locals>.inner``."""
    source = "def outer():\n    def helper(): ...\n    def inner():\n        helper()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {
        ("pkg.m:outer.<locals>.inner", "pkg.m:outer.<locals>.helper")
    }


def test_package_attribute_through_reexport_and_star_reexport() -> None:
    """``pkg.f()`` follows a named re-export; a star re-export is not followed (documented)."""
    sources = {
        "pkg/__init__.py": "from pkg.core import f\nfrom pkg.extra import *\n",
        "pkg/core.py": "def f(): ...\n",
        "pkg/extra.py": "def g(): ...\n",
        "pkg/user.py": "import pkg\ndef a():\n    pkg.f()\n    pkg.g()\n",
    }
    assert pairs_of(sources) == {("pkg.user:a", "pkg.core:f")}


def test_multiple_inheritance_generic_base_and_explicit_super() -> None:
    """``C(A, B)`` finds A's method first; ``Base[int]`` is a base; ``super(C, self)`` works."""
    source = (
        "from typing import Generic, TypeVar\nT = TypeVar('T')\n"
        "class A:\n    def m(self): ...\n"
        "class B:\n    def m(self): ...\n    def only_b(self): ...\n"
        "class Box(Generic[T]):\n    def put(self): ...\n"
        "class C(A, B, Box[int]):\n"
        "    def run(self):\n        self.m()\n        self.only_b()\n        self.put()\n"
        "    def m(self):\n        super(C, self).m()\n"
    )
    pairs = pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert {
        ("pkg.m:C.run", "pkg.m:C.m"),
        ("pkg.m:C.run", "pkg.m:B.only_b"),
        ("pkg.m:C.run", "pkg.m:Box.put"),
        ("pkg.m:C.m", "pkg.m:A.m"),
    } <= pairs


def test_annotated_assignments_and_chained_calls() -> None:
    """``g: G = ...``, ``self.attr: G = ...`` in a method, and ``self.factory.build().run()``."""
    source = (
        "class G:\n    def run(self): ...\n"
        "class Factory:\n    def build(self) -> G: ...\n"
        "class Holder:\n"
        "    def __init__(self, source):\n        self.factory = Factory()\n        self.kept: G = source\n"
        "    def go(self, source):\n"
        "        local: G = source\n        local.run()\n        self.kept.run()\n        self.factory.build().run()\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:Holder.go", "pkg.m:G.run")] == (INFERRED, (11, 12, 13))


def test_unannotated_parameter_gives_no_edge() -> None:
    """Without an annotation, ``g.add()`` cannot be resolved."""
    source = "class G:\n    def add(self): ...\ndef f(g):\n    g.add()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == set()


def test_async_functions() -> None:
    """``async def`` callers and callees work like plain functions."""
    source = "async def fetch(): ...\nasync def main():\n    await fetch()\n"
    assert pairs_of({"pkg/__init__.py": "", "pkg/m.py": source}) == {("pkg.m:main", "pkg.m:fetch")}


# --- second review: Self receivers, closures in class methods, properties -------

SELF_SOURCE = (
    "from typing import Optional, Self\n"
    "class Base:\n"
    "    def copy(self) -> Self: ...\n"
    "    def maybe(self) -> Optional[Self]: ...\n"
    "    @classmethod\n    def create(cls) -> Self: ...\n"
    "class Sub(Base):\n    def sub_only(self): ...\n"
)


def test_self_is_the_receiver_class_not_the_defining_class() -> None:
    """``s.copy()``, ``s.maybe()`` and ``Sub.create()`` are ``Sub``, though defined on ``Base``."""
    source = SELF_SOURCE + "def f(s: Sub):\n    s.copy().sub_only()\n    s.maybe().sub_only()\n    Sub.create().sub_only()\n"
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:f", "pkg.m:Sub.sub_only")] == (INFERRED, (10, 11, 12))


def test_long_fluent_chain_is_fast() -> None:
    """A 30-call ``-> Self`` chain resolves (it took 2**30 steps before the fix)."""
    chain = "".join(".copy()" for _ in range(30))
    source = SELF_SOURCE + f"def f(s: Sub):\n    s{chain}.sub_only()\n"
    assert ("pkg.m:f", "pkg.m:Sub.sub_only") in pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})


def test_cls_from_a_closure_inside_a_classmethod() -> None:
    """``cls()`` and ``cls.other()`` inside a function nested in a class method."""
    source = (
        "class G:\n    def __init__(self): ...\n"
        "    @classmethod\n    def other(cls): ...\n"
        "    @classmethod\n    def make(cls):\n        def build():\n            cls.other()\n            return cls()\n"
    )
    pairs = pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert {("pkg.m:G.make.<locals>.build", "pkg.m:G.other"), ("pkg.m:G.make.<locals>.build", "pkg.m:G.__init__")} <= pairs


def test_nested_function_annotation_names_a_class_of_the_outer_function() -> None:
    """``def inner(item: Local)`` sees ``Local`` defined in the outer function."""
    source = (
        "def outer():\n    class Local:\n        def run(self): ...\n"
        "    def inner(item: Local):\n        item.run()\n"
    )
    pairs = pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert ("pkg.m:outer.<locals>.inner", "pkg.m:outer.<locals>.Local.run") in pairs


def test_cached_property_and_property_with_setter() -> None:
    """Both property kinds type the attribute; a setter does not erase the getter's type."""
    source = (
        "from functools import cached_property\n"
        "class Graph:\n    def add(self): ...\n"
        "class Holder:\n"
        "    @cached_property\n    def cached(self) -> Graph: ...\n"
        "    @property\n    def graph(self) -> Graph: ...\n"
        "    @graph.setter\n    def graph(self, value: Graph) -> None: ...\n"
        "    def go(self):\n        self.cached.add()\n        self.graph.add()\n"
    )
    table = calls_of({"pkg/__init__.py": "", "pkg/m.py": source})
    assert table[("pkg.m:Holder.go", "pkg.m:Graph.add")] == (INFERRED, (12, 13))


def test_module_name_of_unknown_type_does_not_type_functions() -> None:
    """A module-level ``g`` reassigned to something unknown types nothing."""
    source = "class G:\n    def add(self): ...\ng = G()\ng = make_other()\ndef f():\n    g.add()\n"
    assert ("pkg.m:f", "pkg.m:G.add") not in pairs_of({"pkg/__init__.py": "", "pkg/m.py": source})
