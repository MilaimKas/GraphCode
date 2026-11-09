"""Tests for :mod:`graphcode.graph.imports` on the sample fixture and edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from graphcode.graph.definitions import ExtractionError, extract_definitions
from graphcode.graph.imports import ImportAnalysis, extract_imports
from graphcode.graph.model import ArchitectureGraph, EdgeKind, GraphHeader
from graphcode.keys import NodeKey

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def analyse(file_paths: list[str], read_source: object) -> tuple[ImportAnalysis, ArchitectureGraph]:
    """Run definitions + imports and build a graph, which checks the model rules."""
    nodes = extract_definitions(file_paths, read_source)  # type: ignore[arg-type]
    analysis = extract_imports(nodes, read_source)  # type: ignore[arg-type]
    graph = ArchitectureGraph(GraphHeader.for_current_build(None, False), nodes, analysis.edges)
    return analysis, graph


def edge_pairs(analysis: ImportAnalysis) -> set[tuple[str, str]]:
    """Return import edges as (source, target) text pairs."""
    assert all(edge.kind == EdgeKind.IMPORTS for edge in analysis.edges)
    return {(str(edge.source), str(edge.target)) for edge in analysis.edges}


def name_tables(analysis: ImportAnalysis) -> dict[str, dict[str, str]]:
    """Return name tables with keys as text."""
    return {
        str(scope): {name: str(key) for name, key in table.items()} for scope, table in analysis.names.items()
    }


def analyse_sources(sources: dict[str, str]) -> ImportAnalysis:
    """Analyse in-memory sources."""
    return analyse(list(sources), sources.__getitem__)[0]


# --- golden answer on the fixture --------------------------------------------


@pytest.fixture(scope="module")
def fixture_analysis() -> ImportAnalysis:
    """Analyse the fixture package once."""
    paths = [path.relative_to(FIXTURE_ROOT).as_posix() for path in (FIXTURE_ROOT / "sample").rglob("*.py")]
    return analyse(paths, lambda path: (FIXTURE_ROOT / path).read_text(encoding="utf-8"))[0]


def test_fixture_import_edges(fixture_analysis: ImportAnalysis) -> None:
    """Runtime imports inside the package; TYPE_CHECKING and external ones dropped."""
    assert edge_pairs(fixture_analysis) == {
        ("sample", "sample.builders"),
        ("sample", "sample.core"),
        ("sample.builders", "sample.core"),
        ("sample.builders", "sample.utils"),
        ("sample.builders:build_noisy", "sample.noise.gaussian"),  # import inside a function
        ("sample.noise.gaussian", "sample.noise.base"),  # relative import
    }


def test_fixture_name_tables(fixture_analysis: ImportAnalysis) -> None:
    """Every in-package imported name resolves to the node that defines it."""
    assert name_tables(fixture_analysis) == {
        "sample": {"Graph": "sample.core:Graph", "build_default": "sample.builders:build_default"},
        "sample.core": {"Noise": "sample.noise.base:Noise"},  # TYPE_CHECKING import, kept for annotations
        "sample.builders": {"Graph": "sample.core:Graph", "timed": "sample.utils:timed"},
        "sample.builders:build_noisy": {"GaussianNoise": "sample.noise.gaussian:GaussianNoise"},
        "sample.noise.gaussian": {"Noise": "sample.noise.base:Noise"},
    }


# --- edge cases on synthetic sources -----------------------------------------

PACKAGE = {
    "pkg/__init__.py": "from pkg.sub import Thing\n",
    "pkg/sub/__init__.py": "from .mod import Thing\nfrom . import mod\n",
    "pkg/sub/mod.py": "class Thing: ...\n",
}


def test_plain_import_binds_top_package_but_depends_on_full_module() -> None:
    """``import pkg.sub.mod`` binds ``pkg`` and depends on ``pkg.sub.mod``."""
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": "import pkg.sub.mod\nimport pkg.sub.mod as alias\n"})
    assert ("pkg.user", "pkg.sub.mod") in edge_pairs(analysis)
    assert name_tables(analysis)["pkg.user"] == {"pkg": "pkg", "alias": "pkg.sub.mod"}


def test_reexport_chain_is_followed() -> None:
    """``from pkg import Thing`` goes pkg -> pkg.sub -> pkg.sub.mod:Thing."""
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": "from pkg import Thing\n"})
    assert name_tables(analysis)["pkg.user"] == {"Thing": "pkg.sub.mod:Thing"}
    assert ("pkg.user", "pkg") in edge_pairs(analysis)


def test_from_package_import_submodule() -> None:
    """``from . import mod`` in a package __init__ depends on the submodule."""
    analysis = analyse_sources(PACKAGE)
    assert ("pkg.sub", "pkg.sub.mod") in edge_pairs(analysis)
    assert name_tables(analysis)["pkg.sub"]["mod"] == "pkg.sub.mod"


def test_relative_import_two_levels_up() -> None:
    """``from ..sub.mod import Thing`` from pkg/other/user.py."""
    sources = {**PACKAGE, "pkg/other/__init__.py": "", "pkg/other/user.py": "from ..sub.mod import Thing\n"}
    assert name_tables(analyse_sources(sources))["pkg.other.user"] == {"Thing": "pkg.sub.mod:Thing"}


def test_relative_import_above_top_package_raises() -> None:
    """Going above the analysed package is an error, with file and line."""
    with pytest.raises(ExtractionError, match="pkg/user.py:1"):
        analyse_sources({"pkg/__init__.py": "", "pkg/user.py": "from ... import x\n"})


def test_type_checking_import_has_no_edge() -> None:
    """Both spellings of TYPE_CHECKING are recognised; the name is still kept."""
    source = "import typing\nif typing.TYPE_CHECKING:\n    from pkg.sub.mod import Thing\n"
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": source})
    assert not any(pair[0] == "pkg.user" for pair in edge_pairs(analysis))
    assert name_tables(analysis)["pkg.user"] == {"Thing": "pkg.sub.mod:Thing"}


def test_external_and_star_imports_are_dropped() -> None:
    """numpy and the standard library produce nothing; ``import *`` only an edge."""
    source = "import numpy as np\nfrom os import path\nfrom pkg.sub.mod import *\n"
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": source})
    assert "pkg.user" not in name_tables(analysis)
    assert ("pkg.user", "pkg.sub.mod") in edge_pairs(analysis)


def test_reexported_name_wins_over_submodule() -> None:
    """``from .main import main`` in __init__: ``from pkg import main`` is the function."""
    sources = {
        "pkg/__init__.py": "from .main import main\n",
        "pkg/main.py": "def main(): ...\n",
        "pkg/cli.py": "from pkg import main\n",
    }
    analysis = analyse_sources(sources)
    assert name_tables(analysis)["pkg.cli"] == {"main": "pkg.main:main"}
    assert name_tables(analysis)["pkg"] == {"main": "pkg.main:main"}
    assert ("pkg.cli", "pkg") in edge_pairs(analysis)


def test_star_reexport_is_followed() -> None:
    """``from .core import *`` in __init__ re-exports core's definitions."""
    sources = {
        "pkg/__init__.py": "from .core import *\n",
        "pkg/core.py": "class Graph: ...\n",
        "pkg/user.py": "from pkg import Graph\n",
    }
    analysis = analyse_sources(sources)
    assert name_tables(analysis)["pkg.user"] == {"Graph": "pkg.core:Graph"}
    assert ("pkg", "pkg.core") in edge_pairs(analysis)


def test_external_names_are_recorded_per_scope() -> None:
    """A local external import is recorded where it shadows a package name."""
    source = "from pkg.sub.mod import Thing as load\nimport numpy as np\ndef f():\n    from json import load\n"
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": source})
    assert analysis.external_names[NodeKey.from_string("pkg.user")] == frozenset({"np"})
    assert analysis.external_names[NodeKey.from_string("pkg.user:f")] == frozenset({"load"})


def test_scope_keys_of_imports_in_methods_and_nested_functions() -> None:
    """Imports get the exact keys definitions.py gives their scopes."""
    source = (
        "class A:\n"
        "    def m(self):\n"
        "        from pkg.sub.mod import Thing\n"
        "def outer():\n"
        "    def inner():\n"
        "        from pkg.sub import mod\n"
    )
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": source})
    assert ("pkg.user:A.m", "pkg.sub.mod") in edge_pairs(analysis)
    assert ("pkg.user:outer.<locals>.inner", "pkg.sub.mod") in edge_pairs(analysis)


def test_try_except_import_fallback() -> None:
    """Both branches of a try/except ImportError are module-level imports."""
    source = "try:\n    from pkg.sub.mod import Thing\nexcept ImportError:\n    Thing = None\n"
    analysis = analyse_sources({**PACKAGE, "pkg/user.py": source})
    assert name_tables(analysis)["pkg.user"] == {"Thing": "pkg.sub.mod:Thing"}


def test_circular_reexport_terminates() -> None:
    """Two modules re-exporting a name from each other resolve to nothing."""
    sources = {"pkg/__init__.py": "", "pkg/a.py": "from pkg.b import x\n", "pkg/b.py": "from pkg.a import x\n"}
    assert "x" not in name_tables(analyse_sources(sources)).get("pkg.a", {})
