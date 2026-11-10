"""Tests for config, sources, inheritance edges and the end-to-end graph build."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from graphcode.config import ConfigError, load_config
from graphcode.graph.build import build_graph
from graphcode.graph.definitions import extract_definitions
from graphcode.graph.imports import extract_imports
from graphcode.graph.inheritance import extract_inheritance
from graphcode.graph.model import EdgeKind
from graphcode.sources import SourceError, find_package_files, git_state

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def write_project(root: Path, files: dict[str, str]) -> Path:
    """Write a small project into ``root`` and return it."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# --- config --------------------------------------------------------------------


def test_load_config_of_the_fixture() -> None:
    """The fixture configures ``package = "sample"``."""
    config = load_config(FIXTURE_ROOT)
    assert (config.package, config.exclude) == ("sample", ())
    assert config.output_folder == FIXTURE_ROOT / ".graphcode"


def test_scenarios_table_is_passed_on(tmp_path: Path) -> None:
    """``[tool.graphcode.scenarios]`` arrives unchanged, for trace.targets to read."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.graphcode]\npackage = "a"\n[tool.graphcode.scenarios]\nsmall = "s:f"\nbig = { target = "s:g", args = [1] }\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path).scenarios == {"small": "s:f", "big": {"target": "s:g", "args": [1]}}


@pytest.mark.parametrize(
    ("pyproject", "message"),
    [
        (None, "no pyproject.toml"),
        ("[project]\nname = 'x'\n", "no \\[tool.graphcode\\] section"),
        ("[tool.graphcode]\npackage = 'a.b'\n", "top-level import name"),
        ("[tool.graphcode]\npackage = 'a'\nexclude = 'a/x'\n", "list of paths"),
        ("[tool.graphcode\n", "cannot read"),
        ("[tool.graphcode]\npackage = 'a'\nscenarios = 'x'\n", "table of name"),
    ],
)
def test_invalid_config(tmp_path: Path, pyproject: str | None, message: str) -> None:
    """Each configuration mistake gives a clear error."""
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


# --- sources -------------------------------------------------------------------


def test_src_layout_and_exclude(tmp_path: Path) -> None:
    """``src/<pkg>`` is found; excluded folders and files are left out."""
    write_project(
        tmp_path,
        {"src/pkg/__init__.py": "", "src/pkg/a.py": "", "src/pkg/legacy/__init__.py": "", "src/pkg/legacy/old.py": ""},
    )
    layout = find_package_files(tmp_path, "pkg", ["src/pkg/legacy"])
    assert (layout.source_root, layout.files) == ("src", ("src/pkg/__init__.py", "src/pkg/a.py"))


def test_missing_package(tmp_path: Path) -> None:
    """A package that is neither in src/ nor at the root is an error."""
    with pytest.raises(SourceError, match="not found"):
        find_package_files(tmp_path, "pkg")


def test_git_state_outside_a_repository(tmp_path: Path) -> None:
    """Outside git: no commit, not dirty."""
    assert git_state(tmp_path) == (None, False)


# --- inheritance -----------------------------------------------------------------


def test_inheritance_edges() -> None:
    """In-package bases only, generic bases by their class, one line per class."""
    sources = {
        "pkg/__init__.py": "",
        "pkg/base.py": "class Base: ...\nclass Box: ...\n",
        "pkg/m.py": (
            "from pkg.base import Base, Box\n"
            "class A(Base, Exception): ...\n"
            "class B(A, Box[int]): ...\n"
        ),
    }
    nodes = extract_definitions(list(sources), sources.__getitem__)
    edges = extract_inheritance(nodes, sources.__getitem__, extract_imports(nodes, sources.__getitem__))
    assert {(str(edge.source), str(edge.target), edge.lines) for edge in edges} == {
        ("pkg.m:A", "pkg.base:Base", (2,)),
        ("pkg.m:B", "pkg.m:A", (3,)),
        ("pkg.m:B", "pkg.base:Box", (3,)),
    }
    assert all(edge.kind == EdgeKind.INHERITS for edge in edges)


# --- end to end ------------------------------------------------------------------


def test_build_graph_of_the_fixture() -> None:
    """33 nodes; 21 calls, 6 imports and 1 inheritance edge (GaussianNoise -> Noise)."""
    graph = build_graph(load_config(FIXTURE_ROOT))
    assert len(graph.nodes) == 33
    assert Counter(edge.kind for edge in graph.edges) == {EdgeKind.CALLS: 21, EdgeKind.IMPORTS: 6, EdgeKind.INHERITS: 1}
    inherits = [edge for edge in graph.edges if edge.kind == EdgeKind.INHERITS]
    assert (str(inherits[0].source), str(inherits[0].target)) == (
        "sample.noise.gaussian:GaussianNoise",
        "sample.noise.base:Noise",
    )
