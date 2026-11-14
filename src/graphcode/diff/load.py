"""The two graphs to compare: parse ``main`` or ``main..feature``, build or reuse each graph.

A commit never changes, so its graph is cached in ``.graphcode/cache/<commit>.json``
and reused while its fingerprints were made the same way as now (same
graphcode fingerprint rule, same Python version). The working tree changes all
the time and is never cached.
"""

from __future__ import annotations

from pathlib import Path

from graphcode.config import GraphCodeConfig
from graphcode.graph.build import build_graph
from graphcode.graph.fingerprint import fingerprint_scheme
from graphcode.graph.model import ArchitectureGraph, GraphFormatError
from graphcode.graph.serialization import read_graph, write_graph
from graphcode.sources import resolve_commit

CACHE_FOLDER = "cache"
RANGE_SEPARATOR = ".."


def parse_range(text: str) -> tuple[str, str | None]:
    """Split ``"main..feature"`` into ``("main", "feature")``; ``"main"`` gives ``("main", None)``.

    None stands for the working tree (uncommitted changes included).

    Raises
    ------
    ValueError
        If one side of ``..`` is empty.
    """
    if RANGE_SEPARATOR not in text:
        return text, None
    base, head = text.split(RANGE_SEPARATOR, 1)
    if not base or not head:
        raise ValueError(f"expected <ref> or <ref>..<ref>, got {text!r}")
    return base, head


def graph_at(config: GraphCodeConfig, ref: str | None) -> ArchitectureGraph:
    """The graph of a branch, tag or commit (cached), or of the working tree when ``ref`` is None.

    Raises
    ------
    SourceError
        If git does not know ``ref``, or the package is missing at that commit.
    ExtractionError
        If a file of that version cannot be parsed.
    """
    if ref is None:
        return build_graph(config)
    commit = resolve_commit(config.project_root, ref)
    cache_file = config.output_folder / CACHE_FOLDER / f"{commit}.json"
    cached = _read_cached(cache_file)
    if cached is not None:
        return cached
    graph = build_graph(config, ref=commit)
    write_graph(graph, cache_file)
    return graph


def _read_cached(cache_file: Path) -> ArchitectureGraph | None:
    """The cached graph, if present, readable and made with today's fingerprint scheme."""
    if not cache_file.is_file():
        return None
    try:
        graph = read_graph(cache_file)
    except GraphFormatError:  # an older file format: rebuild
        return None
    return graph if graph.header.fingerprint_scheme == fingerprint_scheme() else None
