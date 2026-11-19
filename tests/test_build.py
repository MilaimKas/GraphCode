"""Tests for config, sources, inheritance edges and the end-to-end graph build."""

from __future__ import annotations

import subprocess
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
        ("[tool.graphcode]\n", "has no package"),
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


def test_package_override_needs_no_pyproject(tmp_path: Path) -> None:
    """``--package`` works in a folder without ``pyproject.toml``, with default settings."""
    config = load_config(tmp_path, package="other")
    assert (config.package, config.exclude, config.scenarios) == ("other", (), {})


def test_package_override_without_section(tmp_path: Path) -> None:
    """A repository you don't own: a ``pyproject.toml`` without the section, plus ``--package``."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'theirs'\n", encoding="utf-8")
    assert load_config(tmp_path, package="theirs").package == "theirs"


def test_package_override_keeps_the_other_settings(tmp_path: Path) -> None:
    """Only ``package`` is replaced; ``exclude`` and ``scenarios`` are still read."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.graphcode]\npackage = "a"\nexclude = ["a/old/"]\n[tool.graphcode.scenarios]\nsmall = "s:f"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path, package="b")
    assert (config.package, config.exclude, config.scenarios) == ("b", ("a/old",), {"small": "s:f"})


def test_invalid_package_override(tmp_path: Path) -> None:
    """A dotted ``--package`` is refused like a dotted setting."""
    with pytest.raises(ConfigError, match="top-level import name"):
        load_config(tmp_path, package="a.b")


def test_output_override_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative ``--output`` is taken from the current folder, not from the project."""
    monkeypatch.chdir(tmp_path)
    config = load_config(FIXTURE_ROOT, output_folder=Path("out"))
    assert config.output_folder == tmp_path / "out"
    assert load_config(FIXTURE_ROOT).output_folder == FIXTURE_ROOT / ".graphcode"


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


# --- reading a commit from git -----------------------------------------------------------


def git(root: Path, *arguments: str) -> str:
    """Run git in ``root`` with a fixed identity; return its output."""
    command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    return subprocess.run([*command, *arguments], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A git repository whose project lives in a subfolder; ``v1`` then a working-tree change."""
    project = write_project(
        tmp_path / "project",
        {
            "pyproject.toml": "[tool.graphcode]\npackage = 'pkg'\n",
            "src/pkg/__init__.py": "",
            "src/pkg/a.py": "def f():\n    return g()\n\ndef g():\n    return 1\n",
        },
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "v1")
    git(tmp_path, "tag", "v1")
    (project / "src/pkg/a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (project / "src/pkg/b.py").write_text("def h():\n    pass\n", encoding="utf-8")
    return project


def test_graph_of_a_commit_ignores_the_working_tree(repository: Path) -> None:
    """``ref='v1'`` reads the committed files, even from a project in a subfolder of the repository."""
    config = load_config(repository)
    committed = build_graph(config, ref="v1")
    current = build_graph(config)
    assert {str(node.key) for node in committed.nodes} == {"pkg", "pkg.a", "pkg.a:f", "pkg.a:g"}
    assert [(str(edge.source), str(edge.target)) for edge in committed.edges] == [("pkg.a:f", "pkg.a:g")]
    assert committed.header.commit == git(repository, "rev-parse", "v1") and committed.header.dirty is False
    assert "pkg.b:h" in {str(node.key) for node in current.nodes} and current.header.dirty


def test_unknown_ref(repository: Path) -> None:
    """A ref git does not know is a SourceError naming it."""
    with pytest.raises(SourceError, match="unknown git ref 'nope'"):
        build_graph(load_config(repository), ref="nope")
