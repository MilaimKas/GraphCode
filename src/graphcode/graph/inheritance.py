"""Extract inheritance edges: one ``INHERITS`` edge from a class to each in-package base.

Bases outside the package (``Exception``, ``Generic[T]``, numpy classes) give
no edge. A generic base ``Base[int]`` points to ``Base``.
"""

from __future__ import annotations

from collections.abc import Sequence

from graphcode.graph.definitions import SourceReader
from graphcode.graph.imports import ImportAnalysis
from graphcode.graph.model import Confidence, Edge, EdgeKind, Node
from graphcode.graph.names import PackageNames


def extract_inheritance(nodes: Sequence[Node], read_source: SourceReader, imports: ImportAnalysis) -> tuple[Edge, ...]:
    """Return the inheritance edges of the extracted package.

    Parameters
    ----------
    nodes : sequence of Node
        Nodes from :func:`graphcode.graph.definitions.extract_definitions`.
    read_source : SourceReader
        Returns a file's text from its project-relative path.
    imports : ImportAnalysis
        Name tables from :func:`graphcode.graph.imports.extract_imports`.

    Returns
    -------
    tuple of Edge
        ``INHERITS`` edges, subclass -> base, sorted; the line is the
        ``class`` statement's.
    """
    names = PackageNames(nodes, read_source, imports)
    edges = []
    for class_key, statements in sorted(names.classes.items()):
        for base in names.bases(class_key):
            edges.append(Edge(class_key, base, EdgeKind.INHERITS, Confidence.EXACT, (statements[-1].lineno,)))
    return tuple(edges)
