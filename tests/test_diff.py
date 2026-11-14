"""Tests for the PR diff: comparing the graphs of two versions of a small package."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from graphcode.cli import main as cli_main
from graphcode.config import load_config
from graphcode.diff.compare import IncomparableGraphsError, NodeChange, compare_graphs
from graphcode.diff.load import graph_at, parse_range
from graphcode.graph.build import _extract
from graphcode.graph.model import ArchitectureGraph, GraphHeader, Node
from graphcode.graph.serialization import read_graph, write_graph


def graph_of(files: dict[str, str]) -> ArchitectureGraph:
    """Graph of an in-memory package (paths -> source text)."""
    nodes, edges = _extract(tuple(files), "", files.__getitem__)
    return ArchitectureGraph(GraphHeader.for_current_build(None, False), nodes, edges)


BASE = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "from pkg.util import clean\n\n"
        "def run(x):\n    return clean(x) + 1\n\n"
        "def stub():\n    pass\n\n"
        "def other_stub():\n    pass\n"
    ),
    "pkg/util.py": "def clean(x):\n    return x.strip()\n",
}


def changed(**files: str) -> dict[str, str]:
    """BASE with some files replaced (keyword names use _ for /, e.g. pkg_core)."""
    return {**BASE, **{name.replace("_", "/", 1) + ".py": text for name, text in files.items()}}


def keys(nodes_or_changes: tuple[Node, ...] | tuple[NodeChange, ...]) -> list[str]:
    """Readable keys of nodes, or "before -> after" for node changes."""
    return [
        f"{item.before.key} -> {item.after.key}" if hasattr(item, "before") else str(item.key)
        for item in nodes_or_changes
    ]


def test_identical_and_reformatted_versions_have_no_diff() -> None:
    """Formatting and comments are no change; nothing is reported."""
    reformatted = changed(pkg_util="def clean( x ):\n    # trim\n    return x.strip( )\n")
    assert compare_graphs(graph_of(BASE), graph_of(reformatted)).is_empty


def test_modified_added_removed_and_edges() -> None:
    """A changed body is modified; a new function is added; calls are compared by (source, target, kind)."""
    core = BASE["pkg/core.py"].replace("return clean(x) + 1", "return helper(clean(x))") + "\ndef helper(y):\n    return y * 2\n"
    diff = compare_graphs(graph_of(BASE), graph_of(changed(pkg_core=core)))
    assert keys(diff.modified) == ["pkg.core:run -> pkg.core:run"]
    assert keys(diff.added) == ["pkg.core:helper"] and diff.removed == ()
    assert [(str(edge.source), str(edge.target)) for edge in diff.added_edges] == [("pkg.core:run", "pkg.core:helper")]
    assert diff.removed_edges == () and diff.new_dependencies == ()


def test_unique_same_structure_is_a_move() -> None:
    """A renamed function keeps its fingerprint: one move, not a removal and an addition."""
    renamed = changed(pkg_util="def tidy(x):\n    return x.strip()\n")
    renamed["pkg/core.py"] = BASE["pkg/core.py"].replace("clean", "tidy")
    diff = compare_graphs(graph_of(BASE), graph_of(renamed))
    assert keys(diff.moved) == ["pkg.util:clean -> pkg.util:tidy"]
    assert diff.added == () and diff.removed == ()


def test_shared_trivial_bodies_are_not_moves() -> None:
    """Two ``pass`` stubs share a fingerprint: renaming both is two removals and two additions."""
    core = BASE["pkg/core.py"].replace("def stub", "def stub_a").replace("def other_stub", "def stub_b")
    diff = compare_graphs(graph_of(BASE), graph_of(changed(pkg_core=core)))
    assert diff.moved == ()
    assert keys(diff.removed) == ["pkg.core:other_stub", "pkg.core:stub"]
    assert keys(diff.added) == ["pkg.core:stub_a", "pkg.core:stub_b"]


def test_new_dependency_from_any_link() -> None:
    """A new call into another module is a new dependency, even through an existing import."""
    util = "from pkg.core import stub\n\ndef clean(x):\n    stub()\n    return x.strip()\n"
    diff = compare_graphs(graph_of(BASE), graph_of(changed(pkg_util=util)))
    assert diff.new_dependencies == (("pkg.util", "pkg.core"),)


def test_new_import_cycle_at_module_level_only() -> None:
    """util importing core at module level closes a cycle; inside a function it does not."""
    at_top = "from pkg.core import stub\n\ndef clean(x):\n    return x.strip()\n"
    in_function = "def clean(x):\n    from pkg.core import stub\n    return x.strip()\n"
    assert compare_graphs(graph_of(BASE), graph_of(changed(pkg_util=at_top))).new_cycles == (("pkg.core", "pkg.util"),)
    assert compare_graphs(graph_of(BASE), graph_of(changed(pkg_util=in_function))).new_cycles == ()


def test_graphs_with_different_fingerprint_schemes_are_refused() -> None:
    """Fingerprints from another Python version would make everything look modified."""
    base = graph_of(BASE)
    other = replace(base, header=replace(base.header, fingerprint_scheme="1-py3.99"))
    with pytest.raises(IncomparableGraphsError, match="rebuild both graphs"):
        compare_graphs(base, other)


def test_unrelated_plain_classes_are_not_a_move() -> None:
    """Two classes with only methods have an empty own body: different method names keep them apart."""
    base = {**BASE, "pkg/cache.py": "class Cache:\n    def get(self):\n        return 1\n"}
    head = {**BASE, "pkg/export.py": "class Exporter:\n    def write(self):\n        return 2\n"}
    diff = compare_graphs(graph_of(base), graph_of(head))
    assert diff.moved == ()
    assert keys(diff.removed) == ["pkg.cache", "pkg.cache:Cache", "pkg.cache:Cache.get"]


def test_a_class_moved_with_its_methods() -> None:
    """A class moved to another module is one move, and so is each of its (unique) methods."""
    cls = "class Cache:\n    def get(self):\n        return 1\n"
    diff = compare_graphs(graph_of({**BASE, "pkg/cache.py": cls}), graph_of({**BASE, "pkg/store.py": cls}))
    assert keys(diff.moved) == ["pkg.cache:Cache -> pkg.store:Cache", "pkg.cache:Cache.get -> pkg.store:Cache.get"]


def test_a_cycle_that_shrank_is_not_new() -> None:
    """a -> b -> c -> a becomes a <-> b: part of the old cycle, so not reported."""
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "import pkg.b\n",
        "pkg/b.py": "import pkg.a\nimport pkg.c\n",
        "pkg/c.py": "import pkg.a\n",
    }
    smaller = {**files, "pkg/c.py": ""}
    assert compare_graphs(graph_of(files), graph_of(smaller)).new_cycles == ()
    assert compare_graphs(graph_of(smaller), graph_of(files)).new_cycles == (("pkg.a", "pkg.b", "pkg.c"),)


# --- loading versions, the report and the command --------------------------------------


def git(root: Path, *arguments: str) -> None:
    """Run git in ``root`` with a fixed identity."""
    command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run([*command, *arguments], cwd=root, capture_output=True, check=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A project in git: tag v1, then commit v2 (util.clean modified, util.extra added)."""
    files = {"pyproject.toml": "[tool.graphcode]\npackage = 'pkg'\n", **BASE}
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "v1")
    git(tmp_path, "tag", "v1")
    (tmp_path / "pkg/util.py").write_text("def clean(x):\n    return x.strip().lower()\n\ndef extra():\n    pass\n")
    git(tmp_path, "commit", "-q", "-am", "v2")
    return tmp_path


@pytest.mark.parametrize(
    ("text", "expected"),
    [("main", ("main", None)), ("main..feature", ("main", "feature")), ("a1b2c3d..HEAD", ("a1b2c3d", "HEAD"))],
)
def test_parse_range(text: str, expected: tuple[str, str | None]) -> None:
    """One ref compares with the working tree; two refs with each other."""
    assert parse_range(text) == expected


def test_parse_range_rejects_an_empty_side() -> None:
    """``main..`` is a mistake, not the working tree."""
    with pytest.raises(ValueError, match="<ref>..<ref>"):
        parse_range("main..")


def test_commit_graphs_are_cached_and_rebuilt_when_stale(repository: Path) -> None:
    """A commit's graph is written once and reused; a cache with another fingerprint scheme is rebuilt."""
    config = load_config(repository)
    first = graph_at(config, "v1")
    cache_file = repository / ".graphcode" / "cache" / f"{first.header.commit}.json"
    assert cache_file.is_file()
    assert graph_at(config, "v1") == first
    stale = read_graph(cache_file)
    write_graph(replace(stale, header=replace(stale.header, fingerprint_scheme="1-py3.0")), cache_file)
    assert graph_at(config, "v1").header.fingerprint_scheme == first.header.fingerprint_scheme


def test_cli_diff_prints_the_markdown_summary(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``graphcode diff v1..HEAD`` lists what v2 changed; v1 against itself has no change."""
    assert cli_main(["diff", "--project", str(repository), "v1..HEAD"]) == 0
    report = capsys.readouterr().out
    assert report.startswith("## graphcode diff: v1 (")
    assert "1 added · 0 removed · 1 modified" in report
    assert "- `pkg.util:extra` (function)" in report and "- `pkg.util:clean` (function)" in report
    assert cli_main(["diff", "--project", str(repository), "v1..v1"]) == 0
    assert "No structural change" in capsys.readouterr().out


def test_cli_diff_unknown_ref(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown ref is a one-line error and exit code 1."""
    assert cli_main(["diff", "--project", str(repository), "nope"]) == 1
    assert "unknown git ref 'nope'" in capsys.readouterr().err
