"""The two graphs to compare: read the version range, build or reuse each graph.

Ranges mean what they mean for ``git diff``::

    main             main's last commit vs the working tree (uncommitted changes included)
    main..feature    the last commits of both: also shows, reversed, what main gained since
    main...feature   where feature left main vs feature's last commit: what the branch
                     itself changed, as a pull request shows it

A commit never changes, so its graph is cached in ``.graphcode/cache/``. The file
name also encodes what else shapes the graph (graphcode version, fingerprint
scheme, ``package`` and ``exclude``), so changing any of them builds a fresh
graph. The working tree changes all the time and is never cached.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from graphcode.config import GraphCodeConfig
from graphcode.graph.build import build_graph
from graphcode.graph.fingerprint import fingerprint_scheme
from graphcode.graph.model import (
    ArchitectureGraph,
    GraphFormatError,
    installed_graphcode_version,
)
from graphcode.graph.serialization import read_graph, write_graph
from graphcode.sources import (
    SourceReader,
    commit_reader,
    merge_base,
    resolve_commit,
    working_tree_reader,
)

CACHE_FOLDER = "cache"


@dataclass(frozen=True)
class VersionRange:
    """The two versions to compare, as the user wrote them.

    Attributes
    ----------
    base, head : str, str or None
        Refs; ``head`` None means the working tree.
    from_branch_point : bool
        True for ``base...head``: compare from where ``head`` left ``base``.
    """

    base: str
    head: str | None
    from_branch_point: bool = False


def parse_range(text: str) -> VersionRange:
    """Read ``main``, ``main..feature`` or ``main...feature``.

    Raises
    ------
    ValueError
        If one side of ``..`` or ``...`` is empty.
    """
    for separator, from_branch_point in (("...", True), ("..", False)):
        if separator in text:
            base, head = text.split(separator, 1)
            if not base or not head:
                raise ValueError(f"expected <ref>, <ref>..<ref> or <ref>...<ref>, got {text!r}")
            return VersionRange(base, head, from_branch_point)
    return VersionRange(text, None)


def base_ref(config: GraphCodeConfig, versions: VersionRange) -> str:
    """The ref whose graph is the "before": ``base``, or the branch point for ``...``."""
    if versions.from_branch_point and versions.head is not None:
        return merge_base(config.project_root, versions.base, versions.head)
    return versions.base


def graph_at(config: GraphCodeConfig, ref: str | None, use_cache: bool = True) -> ArchitectureGraph:
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
    cache_file = config.output_folder / CACHE_FOLDER / f"{commit}-{_settings_code(config)}.json"
    cached = _read_cached(cache_file) if use_cache else None
    if cached is not None:
        return cached
    graph = build_graph(config, ref=commit)
    # Write beside, then rename: an interrupted run never leaves half a file under the real name.
    partial_file = cache_file.with_suffix(".partial")
    write_graph(graph, partial_file)
    os.replace(partial_file, cache_file)
    return graph


def reader_at(config: GraphCodeConfig, ref: str | None) -> SourceReader:
    """A reader of the project's files at ``ref``, or in the working tree when ``ref`` is None."""
    if ref is None:
        return working_tree_reader(config.project_root)
    return commit_reader(config.project_root, resolve_commit(config.project_root, ref))


def _settings_code(config: GraphCodeConfig) -> str:
    """8 characters standing for what, besides the commit, shapes its graph."""
    settings = [installed_graphcode_version(), fingerprint_scheme(), config.package, list(config.exclude)]
    return hashlib.blake2b(json.dumps(settings).encode("utf-8"), digest_size=4).hexdigest()


def _read_cached(cache_file: Path) -> ArchitectureGraph | None:
    """The cached graph, if present and readable (an older format or a damaged file is rebuilt)."""
    if not cache_file.is_file():
        return None
    try:
        return read_graph(cache_file)
    except GraphFormatError:
        return None
