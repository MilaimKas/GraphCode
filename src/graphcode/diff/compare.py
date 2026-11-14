"""Compare the graphs of two versions of the code: what was added, removed, changed or moved.

Nodes are matched by key. Rules:

- **modified**: same key, different structural fingerprint (formatting,
  comments and docstrings do not count, see ``graph/fingerprint.py``);
- **moved** (or renamed): a removed and an added definition with the same
  fingerprint, paired only when that fingerprint belongs to exactly one removed
  and one added definition. Trivial bodies (``pass``, ``...``) share a
  fingerprint across many functions, so a non-unique match is no move;
- **edges**: compared by (source, target, kind); a call moving to another line
  is no change. A moved definition's edges show as removed and added;
- **new dependency**: a pair of modules linked by any edge (import, call or
  inheritance) in the new version but not in the old one;
- **new import cycle**: a group of modules importing each other in a loop, at
  module level, that the old version did not have (a cycle that grew counts).

Example: in ``feature``, ``patterns.create_chain`` now calls ``noise.sample``::

    modified: datagenerator.patterns:create_chain
    added edge: create_chain -> GaussianNoise.sample (calls)
    new dependency: datagenerator.patterns -> datagenerator.noise
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import networkx

from graphcode.graph.model import (
    ArchitectureGraph,
    Edge,
    EdgeKind,
    GraphHeader,
    Node,
    NodeKind,
)

_NEVER_MOVED = {NodeKind.PACKAGE, NodeKind.MODULE}  # a file "move" is a removed and an added module


class IncomparableGraphsError(Exception):
    """Raised when two graphs' fingerprints were made differently (e.g. under two Python versions)."""


@dataclass(frozen=True)
class NodeChange:
    """One definition in both versions: modified in place, or moved to another key."""

    before: Node
    after: Node


@dataclass(frozen=True)
class GraphDiff:
    """Everything that differs between an old (``base``) and a new (``head``) graph.

    Attributes
    ----------
    base, head : GraphHeader
        Where each graph comes from (commit, uncommitted changes).
    added, removed : tuple of Node
        Definitions only in the new, or only in the old graph (moves excluded).
    modified : tuple of NodeChange
        Same key, structure changed.
    moved : tuple of NodeChange
        Same structure, new key (moved to another module or class, or renamed).
    added_edges, removed_edges : tuple of Edge
        Edges only in the new, or only in the old graph.
    new_dependencies : tuple of (str, str)
        (module, module) pairs linked in the new graph only.
    new_cycles : tuple of tuple of str
        Module import cycles of the new graph only, each sorted by name.
    """

    base: GraphHeader
    head: GraphHeader
    added: tuple[Node, ...]
    removed: tuple[Node, ...]
    modified: tuple[NodeChange, ...]
    moved: tuple[NodeChange, ...]
    added_edges: tuple[Edge, ...]
    removed_edges: tuple[Edge, ...]
    new_dependencies: tuple[tuple[str, str], ...]
    new_cycles: tuple[tuple[str, ...], ...]

    @property
    def is_empty(self) -> bool:
        """True when the two graphs have no structural difference."""
        return not (self.added or self.removed or self.modified or self.moved or self.added_edges or self.removed_edges)


def compare_graphs(base: ArchitectureGraph, head: ArchitectureGraph) -> GraphDiff:
    """Return what changed from ``base`` (old) to ``head`` (new).

    Raises
    ------
    IncomparableGraphsError
        If the two graphs' fingerprint schemes differ: every node would look modified.
    """
    if base.header.fingerprint_scheme != head.header.fingerprint_scheme:
        raise IncomparableGraphsError(
            f"fingerprints made differently ({base.header.fingerprint_scheme} vs {head.header.fingerprint_scheme}): "
            "rebuild both graphs with the same graphcode and Python"
        )
    old = {node.key: node for node in base.nodes}
    new = {node.key: node for node in head.nodes}
    modified = tuple(
        NodeChange(old[key], new[key])
        for key in sorted(old.keys() & new.keys())
        if _structure_changed(old[key], new[key])
    )
    removed = [old[key] for key in sorted(old.keys() - new.keys())]
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    moved = _moves(removed, added)
    moved_keys = {change.before.key for change in moved} | {change.after.key for change in moved}
    old_edges = {edge.identity: edge for edge in base.edges}
    new_edges = {edge.identity: edge for edge in head.edges}
    return GraphDiff(
        base=base.header,
        head=head.header,
        added=tuple(node for node in added if node.key not in moved_keys),
        removed=tuple(node for node in removed if node.key not in moved_keys),
        modified=modified,
        moved=moved,
        added_edges=tuple(new_edges[identity] for identity in sorted(new_edges.keys() - old_edges.keys(), key=str)),
        removed_edges=tuple(old_edges[identity] for identity in sorted(old_edges.keys() - new_edges.keys(), key=str)),
        new_dependencies=tuple(sorted(_module_links(head.edges) - _module_links(base.edges))),
        new_cycles=tuple(sorted(_import_cycles(head.edges) - _import_cycles(base.edges))),
    )


def _structure_changed(before: Node, after: Node) -> bool:
    """True when a definition kept its key but changed: fingerprint (if known) or kind."""
    if before.kind != after.kind:
        return True
    return bool(before.fingerprint and after.fingerprint) and before.fingerprint != after.fingerprint


def _moves(removed: Iterable[Node], added: Iterable[Node]) -> tuple[NodeChange, ...]:
    """Pair removed and added definitions with the same fingerprint, when the pairing is unique."""
    removed_by_print: dict[str, list[Node]] = defaultdict(list)
    added_by_print: dict[str, list[Node]] = defaultdict(list)
    for node in removed:
        if node.fingerprint and node.kind not in _NEVER_MOVED:
            removed_by_print[node.fingerprint].append(node)
    for node in added:
        if node.fingerprint and node.kind not in _NEVER_MOVED:
            added_by_print[node.fingerprint].append(node)
    moves = [
        NodeChange(removed_by_print[fingerprint][0], added_by_print[fingerprint][0])
        for fingerprint in removed_by_print.keys() & added_by_print.keys()
        if len(removed_by_print[fingerprint]) == 1 and len(added_by_print[fingerprint]) == 1
    ]
    return tuple(sorted(moves, key=lambda change: change.before.key))


def _module_links(edges: Iterable[Edge]) -> set[tuple[str, str]]:
    """(module, module) pairs joined by any edge between definitions of two different modules."""
    return {
        (edge.source.module, edge.target.module)
        for edge in edges
        if edge.source.module != edge.target.module
    }


def _import_cycles(edges: Iterable[Edge]) -> set[tuple[str, ...]]:
    """Groups of modules importing each other in a loop, from module-level imports only.

    An import inside a function gives a function -> module edge and runs only
    when called, so it cannot make importing fail: it is left out (as are
    ``TYPE_CHECKING`` imports, which give no edge at all).
    """
    imports = networkx.DiGraph()
    imports.add_edges_from(
        (edge.source.module, edge.target.module)
        for edge in edges
        if edge.kind == EdgeKind.IMPORTS and edge.source.is_module and edge.source != edge.target
    )
    # A strongly connected component: modules that can all reach each other through imports.
    return {tuple(sorted(group)) for group in networkx.strongly_connected_components(imports) if len(group) > 1}

