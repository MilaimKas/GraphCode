"""Tests for :mod:`graphcode.graph.definitions` on the sample fixture and edge cases."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from graphcode.graph.definitions import ExtractionError, extract_definitions
from graphcode.graph.model import (
    ArchitectureGraph,
    GraphHeader,
    LineSpan,
    Node,
    NodeKind,
)
from graphcode.keys import NodeKey

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"

# Golden answer: every node of the fixture, by kind (agreed before the code).
EXPECTED_KEYS_BY_KIND: dict[NodeKind, set[str]] = {
    NodeKind.PACKAGE: {"sample", "sample.noise"},
    NodeKind.MODULE: {
        "sample.core",
        "sample.utils",
        "sample.builders",
        "sample.noise.base",
        "sample.noise.gaussian",
    },
    NodeKind.CLASS: {
        "sample.core:GraphError",
        "sample.core:Node",
        "sample.core:Graph",
        "sample.noise.base:Noise",
        "sample.noise.gaussian:GaussianNoise",
        "sample.builders:build_noisy.<locals>.Labeller",
    },
    NodeKind.METHOD: {
        "sample.core:Graph.__init__",
        "sample.core:Graph.add_node",
        "sample.core:Graph.add_edge",
        "sample.core:Graph._check_exists",
        "sample.core:Graph.size",
        "sample.core:Graph.get",
        "sample.noise.base:Noise.__init__",
        "sample.noise.base:Noise.sample",
        "sample.noise.base:Noise.sample_many",
        "sample.noise.gaussian:GaussianNoise.__init__",
        "sample.noise.gaussian:GaussianNoise.sample",
        "sample.noise.gaussian:GaussianNoise.mean_of",
        "sample.builders:build_noisy.<locals>.Labeller.label",
    },
    NodeKind.FUNCTION: {
        "sample.utils:timed",
        "sample.utils:timed.<locals>.wrapper",
        "sample.utils:_elapsed_since",
        "sample.builders:build_default",
        "sample.builders:build_chain",
        "sample.builders:build_chain.<locals>.link",
        "sample.builders:build_noisy",
    },
}


def read_fixture(path: str) -> str:
    """Read a fixture file from its project-relative path."""
    return (FIXTURE_ROOT / path).read_text(encoding="utf-8")


def fixture_file_paths() -> list[str]:
    """Return every .py file of the fixture package, project-relative."""
    return [path.relative_to(FIXTURE_ROOT).as_posix() for path in (FIXTURE_ROOT / "sample").rglob("*.py")]


@pytest.fixture(scope="module")
def fixture_nodes() -> dict[str, Node]:
    """Extract the fixture once and index the nodes by key text."""
    nodes = extract_definitions(fixture_file_paths(), read_fixture)
    return {str(node.key): node for node in nodes}


def line_of(path: str, text: str) -> int:
    """Return the 1-based line of the first line in ``path`` that starts with ``text``."""
    lines = read_fixture(path).splitlines()
    return next(number for number, line in enumerate(lines, start=1) if line.strip().startswith(text))


# --- golden answer on the fixture --------------------------------------------


def test_fixture_has_exactly_the_expected_nodes(fixture_nodes: dict[str, Node]) -> None:
    """The 33 agreed nodes, each with the agreed kind, and nothing else."""
    actual: dict[NodeKind, set[str]] = {kind: set() for kind in NodeKind}
    for key, node in fixture_nodes.items():
        actual[node.kind].add(key)
    assert actual == EXPECTED_KEYS_BY_KIND


def test_fixture_nodes_form_a_valid_graph(fixture_nodes: dict[str, Node]) -> None:
    """The nodes satisfy every model rule (unique keys, containment)."""
    ArchitectureGraph(GraphHeader.for_current_build(None, False), tuple(fixture_nodes.values()), ())


def test_decorated_definitions_start_at_decorator(fixture_nodes: dict[str, Node]) -> None:
    """Spans include decorators: @timed and @dataclass."""
    build_default = fixture_nodes["sample.builders:build_default"]
    assert build_default.spans[0].start == line_of("sample/builders.py", "@timed")
    node_class = fixture_nodes["sample.core:Node"]
    assert node_class.spans[0].start == line_of("sample/core.py", "@dataclass")


def test_property_and_overload_are_merged(fixture_nodes: dict[str, Node]) -> None:
    """Getter + setter give 2 spans; 2 overloads + implementation give 3."""
    assert len(fixture_nodes["sample.core:Graph.size"].spans) == 2
    assert len(fixture_nodes["sample.core:Graph.get"].spans) == 3


def test_module_spans_whole_file(fixture_nodes: dict[str, Node]) -> None:
    """A module node covers line 1 to the last line of its file."""
    line_count = len(read_fixture("sample/core.py").splitlines())
    assert fixture_nodes["sample.core"].spans == (LineSpan(1, line_count),)


def test_parents_follow_nesting(fixture_nodes: dict[str, Node]) -> None:
    """Parents go method -> class -> function -> module -> package -> root."""
    label = fixture_nodes["sample.builders:build_noisy.<locals>.Labeller.label"]
    assert label.parent == NodeKey("sample.builders", "build_noisy.<locals>.Labeller")
    assert fixture_nodes["sample.noise.base"].parent == NodeKey("sample.noise")
    assert fixture_nodes["sample.noise"].parent == NodeKey("sample")
    assert fixture_nodes["sample"].parent is None


# --- edge cases on synthetic sources -----------------------------------------


def extract_sources(sources: dict[str, str], source_root: str = "") -> dict[str, Node]:
    """Extract in-memory sources and index the nodes by key text."""
    nodes = extract_definitions(list(sources), sources.__getitem__, source_root)
    return {str(node.key): node for node in nodes}


def test_definitions_inside_blocks_belong_to_scope() -> None:
    """Defs under if / try / with / match are found, with the scope's key."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    def typed(): ...\n"
        "try:\n"
        "    import fast\n"
        "except ImportError:\n"
        "    def fallback(): ...\n"
        "class C:\n"
        "    match 1:\n"
        "        case 1:\n"
        "            async def run(self): ...\n"
    )
    nodes = extract_sources({"pkg/mod.py": source})
    assert nodes["pkg.mod:typed"].kind == NodeKind.FUNCTION
    assert nodes["pkg.mod:fallback"].kind == NodeKind.FUNCTION
    assert nodes["pkg.mod:C.run"].kind == NodeKind.METHOD


def test_lambdas_and_comprehensions_are_not_nodes() -> None:
    """Only def and class become nodes."""
    nodes = extract_sources({"pkg/mod.py": "f = lambda: 0\ndef g():\n    return [x for x in range(3)]\n"})
    assert set(nodes) == {"pkg.mod", "pkg.mod:g"}


def test_src_layout() -> None:
    """With source_root='src', paths keep src/ but module names do not."""
    nodes = extract_sources({"src/pkg/__init__.py": "", "src/pkg/mod.py": "def f(): ...\n"}, source_root="src")
    assert nodes["pkg.mod"].file == "src/pkg/mod.py"
    assert nodes["pkg.mod"].parent == NodeKey("pkg")
    assert nodes["pkg"].spans == (LineSpan(1, 1),)


def test_redefinition_keeps_last_kind() -> None:
    """A name redefined in one scope is one node, of the surviving kind."""
    nodes = extract_sources({"pkg/mod.py": "class Item: ...\ndef Item(): ...\n"})
    assert nodes["pkg.mod:Item"].kind == NodeKind.FUNCTION
    assert nodes["pkg.mod:Item"].spans == (LineSpan(1, 1), LineSpan(2, 2))


def test_syntax_error_names_the_file() -> None:
    """A file that cannot be parsed gives an ExtractionError with its path."""
    with pytest.raises(ExtractionError, match="pkg/broken.py"):
        extract_sources({"pkg/broken.py": "def f(:\n"})


# --- regression tests from code review ---------------------------------------


@pytest.mark.parametrize(
    ("sources", "source_root"),
    [
        ({"pkg/my-module.py": ""}, ""),
        ({"pkg/1_setup.py": ""}, ""),
        ({"src/pkg/__init__.py": "", "other/run.py": ""}, "src"),
        ({"pkg/__init__.py": "", "pkg/mod.py": "", "pkg/mod/__init__.py": ""}, ""),
    ],
    ids=["dash in name", "leading digit", "outside source root", "module and package clash"],
)
def test_unnameable_files_raise_extraction_error(sources: dict[str, str], source_root: str) -> None:
    """Files that cannot become one unique module name raise ExtractionError."""
    with pytest.raises(ExtractionError):
        extract_sources(sources, source_root)


def test_module_span_counts_lines_like_the_parser() -> None:
    """A form feed is not a line break for Python, so it does not add a line."""
    nodes = extract_sources({"pkg/mod.py": "x = 1\n\x0c\ndef f(): ...\n"})
    assert nodes["pkg.mod"].spans == (LineSpan(1, 3),)


def test_only_real_packages_are_parents() -> None:
    """Without __init__.py, neither a.py nor a namespace folder contains modules."""
    nodes = extract_sources({"a.py": "", "a/b.py": "", "pkg/__init__.py": "", "pkg/sub/mod.py": ""})
    assert nodes["a.b"].parent is None
    assert nodes["pkg.sub.mod"].parent is None


# --- qualname oracle: our keys must equal what Python compiles ---------------

TORTURE_SOURCE = """
class A:
    class B:
        def m(self): ...
    if True:
        def in_if(self): ...
    while condition:  # not "while False": the compiler would drop dead code
        def in_while(self): ...
    else:
        def in_while_else(self): ...
    for _ in ():
        pass
    else:
        def in_for_else(self): ...
    try:
        def in_try(self): ...
    except* ValueError:
        def in_except_star(self): ...
    finally:
        def in_finally(self): ...
    with context:
        def in_with(self): ...
    match 1:
        case 1:
            def in_match(self): ...
    def method(self):
        class D:
            def n(self): ...
        async def inner():
            async with context:
                def in_async_with(): ...
            async for _ in stream:
                def in_async_for(): ...
        return [lambda: 0 for _ in ()]
@decorator
def top():
    return (x for x in ())
square = lambda value: value * value
"""

# Code objects Python creates for expressions, which are deliberately not nodes.
_EXPRESSION_SCOPES = ("<module>", "<lambda>", "<genexpr>", "<listcomp>", "<setcomp>", "<dictcomp>")


def compiled_qualnames(source: str) -> set[str]:
    """Return the co_qualname of every def and class body Python compiles.

    The source is compiled, never executed: nested code objects are found in
    each code object's constants.
    """
    qualnames: set[str] = set()
    pending = [compile(source, "torture.py", "exec")]
    while pending:
        code = pending.pop()
        if not any(marker in code.co_qualname for marker in _EXPRESSION_SCOPES):
            qualnames.add(code.co_qualname)
        pending.extend(constant for constant in code.co_consts if isinstance(constant, type(code)))
    return qualnames


@pytest.mark.skipif(sys.version_info < (3, 11), reason="co_qualname and except* need Python 3.11")
def test_keys_match_compiled_qualnames() -> None:
    """Every def/class key equals the qualified name Python itself assigns."""
    nodes = extract_sources({"pkg/torture.py": TORTURE_SOURCE})
    extracted = {node.key.qualname for node in nodes.values() if not node.key.is_module}
    assert extracted == compiled_qualnames(TORTURE_SOURCE)
