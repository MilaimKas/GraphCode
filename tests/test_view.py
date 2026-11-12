"""Tests for the viewer page and the command line (the JavaScript is checked by hand)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from graphcode.cli import main
from graphcode.view.generate import render_viewer, write_viewer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of the fixture project, so ``.graphcode/`` is written outside the repository."""
    return Path(shutil.copytree(FIXTURE_ROOT, tmp_path / "sample_project"))


def embedded_data(page: str) -> dict:
    """Return the JSON embedded in the page's ``graph-data`` script."""
    match = re.search(r'<script type="application/json" id="graph-data">(.*?)</script>', page, re.DOTALL)
    assert match is not None
    return json.loads(match.group(1))


def test_render_embeds_graph_sources_and_scripts() -> None:
    """Data round-trips through the page; every vendored script is inlined; no placeholder is left."""
    graph = {"nodes": [{"key": "p", "file": "p/__init__.py"}], "edges": []}
    sources = {"p/__init__.py": "x = 1\n"}
    page = render_viewer(graph, sources, "p")
    assert embedded_data(page) == {"graph": graph, "sources": sources, "traces": []}
    assert page.count("<script>") == 7  # dagre, cytoscape, expand-collapse, viewer, trace, network-elements, network
    assert not re.search(r"__(TITLE|STYLE|SCRIPTS|GRAPH_DATA)__", page)


def test_source_text_cannot_break_out_of_the_page() -> None:
    """``</script>`` and a placeholder name inside source code are embedded as plain text."""
    sources = {"p/m.py": 's = "</script><script>alert(1)</script>"\nt = "<!-- <script>"\n# __SCRIPTS__\n'}
    page = render_viewer({"nodes": [], "edges": []}, sources, "<b>p</b>")
    assert embedded_data(page)["sources"] == sources
    assert "<title>&lt;b&gt;p&lt;/b&gt; · graphcode</title>" in page


def test_cli_build_graph_and_view(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``build-graph`` writes graph.json; ``view`` writes the page with every source file."""
    assert main(["build-graph", "--project", str(project)]) == 0
    assert "33 nodes, 28 edges (21 calls, 6 imports, 1 inherits)" in capsys.readouterr().out
    assert (project / ".graphcode" / ".gitignore").read_text() == "*\n"

    assert main(["view", "--no-build", "--project", str(project)]) == 0
    data = embedded_data((project / ".graphcode" / "viewer.html").read_text(encoding="utf-8"))
    assert len(data["graph"]["nodes"]) == 33
    assert set(data["sources"]) == {node["file"] for node in data["graph"]["nodes"]}


def test_cli_reports_configuration_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A project without configuration exits with 1 and a message, not a traceback."""
    assert main(["build-graph", "--project", str(tmp_path)]) == 1
    assert "no pyproject.toml" in capsys.readouterr().err


def test_traces_are_embedded_and_bad_files_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Valid trace files go into the page in name order; broken or outdated ones are skipped with a warning."""
    graph = {"nodes": [{"key": "p", "file": "p/__init__.py", "parent": None}], "edges": []}
    (tmp_path / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    traces = tmp_path / "traces"
    traces.mkdir()
    for name, version in (("b", 1), ("a", 1), ("old", 99)):
        trace = {"header": {"schema_version": version, "scenario": name}, "steps": []}
        (traces / f"{name}.json").write_text(json.dumps(trace), encoding="utf-8")
    (traces / "broken.json").write_text("{not json", encoding="utf-8")

    page = write_viewer(tmp_path / "graph.json", {"p/__init__.py": ""}.__getitem__, tmp_path / "v.html", traces)
    embedded = embedded_data(page.read_text(encoding="utf-8"))["traces"]
    assert [trace["header"]["scenario"] for trace in embedded] == ["a", "b"]
    warnings = capsys.readouterr().err
    assert "skipping broken.json" in warnings and "skipping old.json: schema version 99" in warnings
