"""In-memory model of the structure graph and the rules it must satisfy.

The model is a set of immutable dataclasses. Every rule (unique keys, the
containment rule, one edge per ``(source, target, kind)``) is checked when an
object is created, so an invalid graph cannot exist in memory, whether it was
built by the extractor or read from a file. Reading and writing ``graph.json``
lives in :mod:`graphcode.graph.serialization`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version

from graphcode.keys import NodeKey

SCHEMA_VERSION = 1


class GraphFormatError(ValueError):
    """Raised when a ``graph.json`` file or its content is not valid."""


class NodeKind(str, Enum):
    """What a node represents in the code."""

    PACKAGE = "package"
    MODULE = "module"
    CLASS = "class"
    METHOD = "method"
    FUNCTION = "function"


class EdgeKind(str, Enum):
    """Relation an edge expresses between two nodes."""

    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"


class Confidence(str, Enum):
    """How the extractor found an edge.

    ``EXACT`` edges follow directly from names in the code; ``INFERRED`` edges
    rely on type tracking (e.g. ``dag = DAG(); dag.add_node()``).
    """

    EXACT = "exact"
    INFERRED = "inferred"


@dataclass(frozen=True, order=True)
class LineSpan:
    """Inclusive range of source lines, numbered from 1.

    Attributes
    ----------
    start : int
        First line (the decorator line for a decorated definition).
    end : int
        Last line.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        """Reject empty, reversed or zero-based ranges."""
        if self.start < 1 or self.end < self.start:
            raise GraphFormatError(f"invalid line span: {self.start}-{self.end}")


@dataclass(frozen=True)
class Node:
    """A package, module, class or function.

    Attributes
    ----------
    key : NodeKey
        Join key, unique within a graph.
    kind : NodeKind
        What the node represents.
    file : str
        Source file, relative to the project root, with ``/`` separators.
    spans : tuple of LineSpan
        Line ranges of the definition, sorted. Usually one; several when the
        same key is defined more than once (property setter, ``@overload``).
    parent : NodeKey or None
        Key of the containing node; None only for the top-level package.
    """

    key: NodeKey
    kind: NodeKind
    file: str
    spans: tuple[LineSpan, ...]
    parent: NodeKey | None

    def __post_init__(self) -> None:
        """Require at least one span and store spans sorted, without repeats."""
        if not self.spans:
            raise GraphFormatError(f"node {self.key} has no line span")
        # A frozen dataclass forbids normal assignment; object.__setattr__ is
        # the standard way to normalise a field once, during construction.
        object.__setattr__(self, "spans", tuple(sorted(set(self.spans))))


@dataclass(frozen=True)
class Edge:
    """A directed relation between two nodes, merging all its call sites.

    There is at most one edge per ``(source, target, kind)``: moving a call
    to another line changes ``lines``, not the edge's identity, so the PR
    diff does not report it as removed and added.

    Attributes
    ----------
    source : NodeKey
        Node where the relation starts (the caller, importer or subclass).
    target : NodeKey
        Node the relation points to.
    kind : EdgeKind
        Type of relation.
    confidence : Confidence
        ``EXACT`` if at least one occurrence is exact, else ``INFERRED``.
    lines : tuple of int
        Source lines, in the source node's file, where the relation appears.
    """

    source: NodeKey
    target: NodeKey
    kind: EdgeKind
    confidence: Confidence
    lines: tuple[int, ...]

    def __post_init__(self) -> None:
        """Require at least one valid line and store lines sorted, without repeats."""
        if not self.lines or min(self.lines) < 1:
            raise GraphFormatError(f"edge {self.source} -> {self.target} has invalid lines {self.lines}")
        object.__setattr__(self, "lines", tuple(sorted(set(self.lines))))

    @property
    def identity(self) -> tuple[NodeKey, NodeKey, EdgeKind]:
        """Return what makes the edge unique: ``(source, target, kind)``."""
        return (self.source, self.target, self.kind)


@dataclass(frozen=True)
class GraphHeader:
    """Provenance of a graph: format version and code version it was built from.

    Attributes
    ----------
    schema_version : int
        Version of the file format.
    graphcode_version : str
        Version of graphcode that wrote the file.
    commit : str or None
        Git commit of the analysed code; None outside a git repository.
    dirty : bool
        True when the working tree had uncommitted changes.
    """

    schema_version: int
    graphcode_version: str
    commit: str | None
    dirty: bool

    @classmethod
    def for_current_build(cls, commit: str | None, dirty: bool) -> GraphHeader:
        """Create a header for a graph built now by this graphcode version."""
        return cls(SCHEMA_VERSION, installed_graphcode_version(), commit, dirty)


@dataclass(frozen=True)
class ArchitectureGraph:
    """The full content of one ``graph.json`` file.

    Attributes
    ----------
    header : GraphHeader
        Provenance of the graph.
    nodes : tuple of Node
        All nodes, keys unique. A node's parent is its key minus the last
        level (``NodeKey.parent_key``), or None for a root: the analysed
        package, or a top-level module of an ``include`` folder.
    edges : tuple of Edge
        All edges, unique by ``(source, target, kind)``; both ends must be
        nodes of this graph.
    """

    header: GraphHeader
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def __post_init__(self) -> None:
        """Check uniqueness, the containment rule, and that edges point to nodes."""
        known_keys: set[NodeKey] = set()
        for node in self.nodes:
            if node.key in known_keys:
                raise GraphFormatError(f"duplicate node key: {node.key}")
            known_keys.add(node.key)
        for node in self.nodes:
            self._check_parent(node, known_keys)
        seen_identities: set[tuple[NodeKey, NodeKey, EdgeKind]] = set()
        for edge in self.edges:
            if edge.identity in seen_identities:
                raise GraphFormatError(f"duplicate edge: {edge.source} -{edge.kind.value}-> {edge.target}")
            seen_identities.add(edge.identity)
            for end in (edge.source, edge.target):
                if end not in known_keys:
                    raise GraphFormatError(f"edge {edge.source} -> {edge.target}: unknown node {end}")

    @staticmethod
    def _check_parent(node: Node, known_keys: set[NodeKey]) -> None:
        """Enforce the containment rule; it also makes cycles impossible.

        Because a parent key is always strictly shorter than its child's key,
        following parents always ends at a root.
        """
        if node.parent is None:
            if node.kind not in (NodeKind.PACKAGE, NodeKind.MODULE):
                raise GraphFormatError(f"{node.kind.value} node {node.key} has no parent")
            return
        if node.parent != node.key.parent_key():
            raise GraphFormatError(f"node {node.key} cannot have parent {node.parent}")
        if node.parent not in known_keys:
            raise GraphFormatError(f"node {node.key} has unknown parent {node.parent}")

    def node_by_key(self) -> dict[NodeKey, Node]:
        """Return a lookup table from key to node."""
        return {node.key: node for node in self.nodes}


def installed_graphcode_version() -> str:
    """Return the installed graphcode version, or ``"unknown"`` if not installed."""
    try:
        return version("graphcode")
    except PackageNotFoundError:
        return "unknown"

