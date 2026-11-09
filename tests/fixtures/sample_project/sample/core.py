"""Core data structure of the sample package.

Covers: classes with methods, a dataclass (decorated class), a property with
a setter (duplicate key, two spans), ``@overload`` stubs (duplicate key, three
spans), a private method, a ``TYPE_CHECKING`` import, ``self.method()`` calls,
an exception class with ``raise`` and ``except``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from sample.noise.base import Noise


class GraphError(Exception):
    """Raised when the graph is used incorrectly."""


@dataclass
class Node:
    """A named node, optionally carrying noise."""

    name: str
    noise: Noise | None = None


class Graph:
    """A minimal directed graph."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[tuple[str, str]] = []

    def add_node(self, name: str) -> Node:
        """Add a node, refusing duplicates."""
        if name in self._nodes:
            raise GraphError(f"duplicate node {name}")
        node = Node(name)
        self._nodes[name] = node
        return node

    def add_edge(self, source: str, target: str) -> None:
        """Add an edge between two existing nodes."""
        self._check_exists(source)
        self._check_exists(target)
        self._edges.append((source, target))

    def _check_exists(self, name: str) -> None:
        if name not in self._nodes:
            raise GraphError(f"unknown node {name}")

    @property
    def size(self) -> int:
        """Number of nodes."""
        return len(self._nodes)

    @size.setter
    def size(self, value: int) -> None:
        raise AttributeError("size is read-only")

    @overload
    def get(self, name: str) -> Node: ...

    @overload
    def get(self, name: int) -> Node: ...

    def get(self, name: str | int) -> Node:
        """Return a node by name or by insertion index."""
        try:
            if isinstance(name, int):
                return list(self._nodes.values())[name]
            return self._nodes[name]
        except (KeyError, IndexError) as error:
            raise GraphError(f"no node {name!r}") from error
