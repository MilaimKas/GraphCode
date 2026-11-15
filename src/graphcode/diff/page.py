"""What the diff page shows: the new graph, the ghosts of what was removed, and code diffs.

The page draws one graph, the union of both versions:

- every node and edge of the new version, with a status: ``added``,
  ``modified``, ``moved-to`` (the new place of a moved definition) or ``unchanged``;
- the ghosts of the old version's nodes and edges that are gone: ``removed``,
  or ``moved-from`` (the old place of a moved definition).

A ghost's code is the old file's text: its ``file`` is prefixed with
``before:``, and that old text is embedded under the prefixed name. A
modified or moved definition also gets a line-by-line diff of its code
(``difflib``), shown in the side panel.

The page data stays plain JSON-like dicts: the ``view`` package reads it
without importing ``graph`` or ``diff``.
"""

from __future__ import annotations

import difflib
from typing import Any

from graphcode.diff.compare import GraphDiff
from graphcode.graph.model import ArchitectureGraph, Edge, EdgeKind, Node
from graphcode.graph.serialization import edge_to_dict, graph_to_dict, node_to_dict
from graphcode.keys import NodeKey
from graphcode.sources import SourceReader

BEFORE_PREFIX = "before:"  # file names of the old version's text, embedded beside the new one


def diff_page_data(
    diff: GraphDiff,
    base: ArchitectureGraph,
    head: ArchitectureGraph,
    read_base: SourceReader,
    read_head: SourceReader,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Return the page's graph, the file texts it needs, and the diff details.

    Parameters
    ----------
    diff : GraphDiff
        The comparison of ``base`` (old) and ``head`` (new).
    base, head : ArchitectureGraph
        The two graphs.
    read_base, read_head : SourceReader
        Read a file of the old and of the new version.

    Returns
    -------
    tuple of (dict, dict, dict)
        Graph in the ``graph.json`` layout with a ``status`` per node and edge;
        file path -> text; ``{"code": {key: diff lines}, "moves": {key: key}}``.
    """
    status = {change.after.key: "modified" for change in diff.modified}
    status.update({node.key: "added" for node in diff.added})
    status.update({change.after.key: "moved-to" for change in diff.moved})
    ghosts = [(node, "removed") for node in diff.removed] + [(change.before, "moved-from") for change in diff.moved]
    added_edges = {edge.identity for edge in diff.added_edges}

    nodes = [node_to_dict(node) | {"status": status.get(node.key, "unchanged")} for node in sorted(head.nodes, key=_by_key)]
    # A ghost's code is the old text, embedded under a "before:" file name.
    nodes += [
        node_to_dict(node) | {"status": label, "file": BEFORE_PREFIX + node.file}
        for node, label in sorted(ghosts, key=lambda ghost: ghost[0].key)
    ]
    edges = [
        edge_to_dict(edge) | {"status": "added" if edge.identity in added_edges else "unchanged"}
        for edge in sorted(head.edges, key=_by_identity)
    ]
    edges += [edge_to_dict(edge) | {"status": "removed"} for edge in diff.removed_edges]
    graph = {"header": graph_to_dict(head)["header"], "nodes": nodes, "edges": edges}

    sources = {path: read_head(path) for path in sorted({node.file for node in head.nodes})}
    old_files = {node.file for node, _ in ghosts} | {change.before.file for change in diff.modified + diff.moved}
    sources.update({BEFORE_PREFIX + path: read_base(path) for path in sorted(old_files)})
    code = {
        str(change.after.key): _code_diff(change.before, change.after, sources[BEFORE_PREFIX + change.before.file], sources[change.after.file])
        for change in diff.modified + diff.moved
    }
    moves = {str(change.before.key): str(change.after.key) for change in diff.moved}
    moves.update({str(change.after.key): str(change.before.key) for change in diff.moved})
    return graph, sources, {"code": code, "moves": moves}


def _by_key(node: Node) -> NodeKey:
    """Sort nodes by key."""
    return node.key


def _by_identity(edge: Edge) -> tuple[NodeKey, NodeKey, EdgeKind]:
    """Sort edges by (source, target, kind)."""
    return edge.identity


def _definition_lines(node: Node, text: str) -> list[str]:
    """The lines of a definition (all its spans), from its file's text."""
    lines = text.splitlines()
    return [line for span in node.spans for line in lines[span.start - 1 : span.end]]


def _code_diff(before: Node, after: Node, before_text: str, after_text: str) -> list[str]:
    """Unified diff of a definition's code, without the two file header lines."""
    diff_lines = difflib.unified_diff(
        _definition_lines(before, before_text), _definition_lines(after, after_text), lineterm="", n=3
    )
    return list(diff_lines)[2:]  # drop "--- " and "+++ ": the panel names the definition
