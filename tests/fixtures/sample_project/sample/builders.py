"""Functions that build sample graphs.

Covers: a decorated function (span starts at the decorator), a nested
function, a class defined inside a function (``f.<locals>.Class.method``), a
lambda (must not become a node), calls on a local variable built from a class
(inferred edges), cross-module calls through ``from ... import``, an import
inside a function (``build_noisy``).
"""

from __future__ import annotations

from sample.core import Graph
from sample.utils import timed


@timed
def build_default() -> Graph:
    """Build a two-node graph with one edge."""
    graph = Graph()
    graph.add_node("x")
    graph.add_node("y")
    graph.add_edge("x", "y")
    return graph


def build_chain(names: list[str]) -> Graph:
    """Build a chain ``a -> b -> c ...`` from the given names."""

    def link(graph: Graph, pairs: list[tuple[str, str]]) -> None:
        for source, target in pairs:
            graph.add_edge(source, target)

    graph = Graph()
    for name in sorted(names, key=lambda text: text.lower()):
        graph.add_node(name)
    link(graph, list(zip(names, names[1:])))
    return graph


def build_noisy(name: str) -> Graph:
    """Build a one-node graph whose node carries Gaussian noise."""
    from sample.noise.gaussian import GaussianNoise

    class Labeller:
        def label(self, text: str) -> str:
            return text.upper()

    graph = Graph()
    node = graph.add_node(Labeller().label(name))
    node.noise = GaussianNoise(scale=0.1)
    return graph
