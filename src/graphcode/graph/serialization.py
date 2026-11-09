"""Reading and writing ``graph.json``: conversion between the model and JSON.

Keys are :class:`NodeKey` objects in Python and plain strings in the file.
Reading validates everything, so a malformed file fails here with one error
type, :class:`GraphFormatError`, never later in the viewer or the diff.

File layout (schema version 1)::

    {
      "header": {"schema_version": 1, "graphcode_version": "0.1.0",
                 "commit": "a1b2c3d", "dirty": false},
      "nodes": [{"key": "pkg.mod:Class", "kind": "class", "file": "pkg/mod.py",
                 "spans": [[31, 210]], "parent": "pkg.mod"}],
      "edges": [{"source": "pkg.mod:f", "target": "pkg.mod:Class", "kind": "calls",
                 "confidence": "exact", "lines": [12, 15]}]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphcode.graph.model import (
    SCHEMA_VERSION,
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
from graphcode.keys import InvalidKeyError, NodeKey


def graph_to_dict(graph: ArchitectureGraph) -> dict[str, Any]:
    """Convert a graph to plain JSON data.

    Nodes and edges are sorted so that the same code always produces the same
    file, which keeps golden-file tests and textual diffs stable.
    """
    header = graph.header
    nodes = sorted(graph.nodes, key=lambda node: node.key)
    edges = sorted(
        graph.edges,
        key=lambda edge: (edge.source, edge.target, edge.kind.value),
    )
    return {
        "header": {
            "schema_version": header.schema_version,
            "graphcode_version": header.graphcode_version,
            "commit": header.commit,
            "dirty": header.dirty,
        },
        "nodes": [
            {
                "key": str(node.key),
                "kind": node.kind.value,
                "file": node.file,
                "spans": [[span.start, span.end] for span in node.spans],
                "parent": None if node.parent is None else str(node.parent),
            }
            for node in nodes
        ],
        "edges": [
            {
                "source": str(edge.source),
                "target": str(edge.target),
                "kind": edge.kind.value,
                "confidence": edge.confidence.value,
                "lines": list(edge.lines),
            }
            for edge in edges
        ],
    }


def graph_from_dict(data: dict[str, Any]) -> ArchitectureGraph:
    """Build a graph from JSON data written by :func:`graph_to_dict`.

    Raises
    ------
    GraphFormatError
        If the schema version differs, a field is missing or has a wrong
        value, or a key is malformed.
    """
    try:
        header_data = data["header"]
        if header_data["schema_version"] != SCHEMA_VERSION:
            raise GraphFormatError(
                f"graph.json has schema version {header_data['schema_version']}, "
                f"this graphcode reads version {SCHEMA_VERSION}; rebuild the graph"
            )
        header = GraphHeader(
            schema_version=header_data["schema_version"],
            graphcode_version=header_data["graphcode_version"],
            commit=header_data["commit"],
            dirty=header_data["dirty"],
        )
        nodes = tuple(
            Node(
                key=NodeKey.from_string(node_data["key"]),
                kind=NodeKind(node_data["kind"]),
                file=node_data["file"],
                spans=tuple(LineSpan(start, end) for start, end in node_data["spans"]),
                parent=None if node_data["parent"] is None else NodeKey.from_string(node_data["parent"]),
            )
            for node_data in data["nodes"]
        )
        edges = tuple(
            Edge(
                source=NodeKey.from_string(edge_data["source"]),
                target=NodeKey.from_string(edge_data["target"]),
                kind=EdgeKind(edge_data["kind"]),
                confidence=Confidence(edge_data["confidence"]),
                lines=tuple(edge_data["lines"]),
            )
            for edge_data in data["edges"]
        )
    except GraphFormatError:
        raise
    # Missing fields (KeyError), wrong types (TypeError, AttributeError),
    # unknown enum values (ValueError) and malformed keys (InvalidKeyError)
    # all become one error type, so callers only handle GraphFormatError.
    except (KeyError, TypeError, AttributeError, ValueError, InvalidKeyError) as error:
        raise GraphFormatError(f"invalid graph.json content: {error!r}") from error
    return ArchitectureGraph(header=header, nodes=nodes, edges=edges)


def write_graph(graph: ArchitectureGraph, path: Path) -> None:
    """Write a graph to ``path`` as UTF-8 JSON, creating parent folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(graph_to_dict(graph), indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def read_graph(path: Path) -> ArchitectureGraph:
    """Read and validate a graph written by :func:`write_graph`.

    Raises
    ------
    GraphFormatError
        If the file is not valid JSON or not a valid graph.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GraphFormatError(f"{path} is not valid UTF-8 JSON: {error}") from error
    return graph_from_dict(data)
