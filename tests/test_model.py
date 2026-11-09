"""Tests for the graph model rules and ``graph.json`` reading and writing."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from graphcode.graph.model import (
    ArchitectureGraph,
    Confidence,
    Edge,
    EdgeKind,
    GraphFormatError,
    GraphHeader,
    LineSpan,
    Node,
    NodeKind,
)
from graphcode.graph.serialization import (
    graph_from_dict,
    graph_to_dict,
    read_graph,
    write_graph,
)
from graphcode.keys import NodeKey

PACKAGE = NodeKey("pkg")
MODULE = NodeKey("pkg.mod")
CLASS = MODULE.child("DAG")
FUNCTION = MODULE.child("build")


def make_nodes() -> list[Node]:
    """Return the nodes of a small valid graph, deliberately not sorted."""
    return [
        Node(FUNCTION, NodeKind.FUNCTION, "pkg/mod.py", (LineSpan(40, 45),), MODULE),
        Node(PACKAGE, NodeKind.PACKAGE, "pkg/__init__.py", (LineSpan(1, 3),), None),
        Node(MODULE, NodeKind.MODULE, "pkg/mod.py", (LineSpan(1, 50),), PACKAGE),
        Node(CLASS, NodeKind.CLASS, "pkg/mod.py", (LineSpan(22, 30), LineSpan(5, 20)), MODULE),
    ]


def make_graph(nodes: list[Node] | None = None, edges: list[Edge] | None = None) -> ArchitectureGraph:
    """Build a graph from the default nodes and one inferred call edge."""
    if edges is None:
        edges = [Edge(FUNCTION, CLASS, EdgeKind.CALLS, Confidence.INFERRED, (45, 41, 41))]
    return ArchitectureGraph(
        header=GraphHeader.for_current_build(commit="a1b2c3d", dirty=False),
        nodes=tuple(make_nodes() if nodes is None else nodes),
        edges=tuple(edges),
    )


# --- normalisation -----------------------------------------------------------


def test_node_spans_are_sorted_deduplicated_and_hashable() -> None:
    """Spans given as an unsorted list with repeats become a sorted tuple."""
    node = Node(CLASS, NodeKind.CLASS, "pkg/mod.py", [LineSpan(50, 60), LineSpan(1, 10), LineSpan(1, 10)], MODULE)
    assert node.spans == (LineSpan(1, 10), LineSpan(50, 60))
    assert hash(node) == hash(node)


def test_edge_lines_are_sorted_and_deduplicated() -> None:
    """Call-site lines are stored sorted, each once."""
    edge = Edge(FUNCTION, CLASS, EdgeKind.CALLS, Confidence.EXACT, (45, 41, 41))
    assert edge.lines == (41, 45)


@pytest.mark.parametrize(("start", "end"), [(0, 3), (5, 2)])
def test_invalid_line_span_rejected(start: int, end: int) -> None:
    """Line spans are 1-based and not reversed."""
    with pytest.raises(GraphFormatError):
        LineSpan(start, end)


# --- graph rules -------------------------------------------------------------


def test_valid_graph_accepted() -> None:
    """The reference graph satisfies every rule."""
    graph = make_graph()
    assert graph.node_by_key()[CLASS].parent == MODULE


def test_duplicate_node_key_rejected() -> None:
    """Two nodes with the same key are an error; the extractor must merge them."""
    nodes = make_nodes()
    with pytest.raises(GraphFormatError, match="duplicate node key"):
        make_graph(nodes=nodes + [nodes[0]])


def test_duplicate_edge_identity_rejected() -> None:
    """One edge per (source, target, kind), even with a different confidence."""
    edges = [
        Edge(FUNCTION, CLASS, EdgeKind.CALLS, Confidence.INFERRED, (41,)),
        Edge(FUNCTION, CLASS, EdgeKind.CALLS, Confidence.EXACT, (45,)),
    ]
    with pytest.raises(GraphFormatError, match="duplicate edge"):
        make_graph(edges=edges)


def test_same_pair_with_different_kind_allowed() -> None:
    """A call and an inheritance between the same two nodes are distinct edges."""
    edges = [
        Edge(FUNCTION, CLASS, EdgeKind.CALLS, Confidence.EXACT, (41,)),
        Edge(FUNCTION, CLASS, EdgeKind.INHERITS, Confidence.EXACT, (40,)),
    ]
    assert len(make_graph(edges=edges).edges) == 2


def test_edge_to_unknown_node_rejected() -> None:
    """Both ends of an edge must be nodes of the graph."""
    edges = [Edge(FUNCTION, MODULE.child("Missing"), EdgeKind.CALLS, Confidence.EXACT, (41,))]
    with pytest.raises(GraphFormatError, match="unknown node"):
        make_graph(edges=edges)


def test_parent_must_be_key_minus_last_level() -> None:
    """A class directly under the package breaks the containment rule."""
    nodes = make_nodes()
    nodes[3] = Node(CLASS, NodeKind.CLASS, "pkg/mod.py", (LineSpan(5, 20),), PACKAGE)
    with pytest.raises(GraphFormatError, match="cannot have parent"):
        make_graph(nodes=nodes)


def test_class_cannot_be_root() -> None:
    """Only packages and modules may have no parent."""
    nodes = make_nodes()
    nodes[3] = Node(CLASS, NodeKind.CLASS, "pkg/mod.py", (LineSpan(5, 20),), None)
    with pytest.raises(GraphFormatError, match="has no parent"):
        make_graph(nodes=nodes)


def test_module_can_be_root() -> None:
    """A module outside any package (folder without __init__.py) is a root."""
    loose = Node(NodeKey("loose"), NodeKind.MODULE, "loose.py", (LineSpan(1, 10),), None)
    graph = make_graph(nodes=make_nodes() + [loose])
    assert graph.node_by_key()[NodeKey("loose")].parent is None


# --- reading and writing -----------------------------------------------------


def test_write_then_read_gives_same_graph(tmp_path: Path) -> None:
    """A graph survives a round trip through graph.json unchanged."""
    graph = make_graph()
    path = tmp_path / "out" / "graph.json"
    write_graph(graph, path)
    assert graph_to_dict(read_graph(path)) == graph_to_dict(graph)


def test_output_is_sorted() -> None:
    """Nodes are written sorted by key, whatever order they were built in."""
    keys = [node["key"] for node in graph_to_dict(make_graph())["nodes"]]
    assert keys == ["pkg", "pkg.mod", "pkg.mod:DAG", "pkg.mod:build"]


def _apply(edit: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """Return a copy of the reference graph's JSON data with ``edit`` applied."""
    data = copy.deepcopy(graph_to_dict(make_graph()))
    edit(data)
    return data


@pytest.mark.parametrize(
    "edit",
    [
        lambda data: data["header"].update(schema_version=2),
        lambda data: data["nodes"][0].update(kind="lambda"),
        lambda data: data["nodes"][0].pop("spans"),
        lambda data: data["nodes"][0].update(spans=[[5, 2]]),
        lambda data: data["nodes"][0].update(key=123),
        lambda data: data["nodes"][2].update(parent="pkg:x y"),
        lambda data: data["edges"][0].update(lines=[]),
        lambda data: data["edges"][0].update(target="pkg.mod:Missing"),
        lambda data: data.pop("edges"),
    ],
    ids=[
        "schema version",
        "unknown kind",
        "missing field",
        "reversed span",
        "key not a string",
        "malformed key",
        "edge without lines",
        "unknown edge target",
        "missing section",
    ],
)
def test_invalid_content_rejected(edit: Callable[[dict[str, Any]], object]) -> None:
    """Every kind of bad content becomes one GraphFormatError."""
    with pytest.raises(GraphFormatError):
        graph_from_dict(_apply(edit))


def test_schema_version_message_says_rebuild() -> None:
    """A version mismatch tells the user what to do."""
    with pytest.raises(GraphFormatError, match="rebuild the graph"):
        graph_from_dict(_apply(lambda data: data["header"].update(schema_version=2)))


@pytest.mark.parametrize("content", [b"\xff", b"{not json"], ids=["not utf-8", "not json"])
def test_unreadable_file_rejected(tmp_path: Path, content: bytes) -> None:
    """Files that are not UTF-8 JSON raise GraphFormatError, not a decode error."""
    path = tmp_path / "graph.json"
    path.write_bytes(content)
    with pytest.raises(GraphFormatError):
        read_graph(path)
