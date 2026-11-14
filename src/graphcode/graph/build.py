"""Build the whole structure graph of a configured project: stage 1 end to end."""

from __future__ import annotations

from graphcode.config import GraphCodeConfig
from graphcode.graph.calls import extract_calls
from graphcode.graph.definitions import extract_definitions
from graphcode.graph.imports import extract_imports
from graphcode.graph.inheritance import extract_inheritance
from graphcode.graph.model import ArchitectureGraph, Edge, GraphHeader, Node
from graphcode.sources import (
    SourceReader,
    commit_reader,
    find_package_files,
    find_package_files_at,
    git_state,
    resolve_commit,
    working_tree_reader,
)


def build_graph(config: GraphCodeConfig, ref: str | None = None) -> ArchitectureGraph:
    """Extract nodes and import, call and inheritance edges from the working tree or a commit.

    Parameters
    ----------
    config : GraphCodeConfig
        Project root, package name and excluded paths (the current ones are
        used for a commit too).
    ref : str or None
        A branch, tag or commit to read from git; None for the working tree.

    Returns
    -------
    ArchitectureGraph
        The validated graph; its header records the git commit and whether
        the package's files had uncommitted changes (never for a commit).
    """
    if ref is not None:
        commit = resolve_commit(config.project_root, ref)
        layout = find_package_files_at(config.project_root, commit, config.package, config.exclude)
        nodes, edges = _extract(layout.files, layout.source_root, commit_reader(config.project_root, commit))
        return ArchitectureGraph(GraphHeader.for_current_build(commit, False), nodes, edges)
    layout = find_package_files(config.project_root, config.package, config.exclude)
    nodes, edges = _extract(layout.files, layout.source_root, working_tree_reader(config.project_root))
    package_folder = f"{layout.source_root}/{config.package}".lstrip("/")
    commit, dirty = git_state(config.project_root, [package_folder])
    return ArchitectureGraph(GraphHeader.for_current_build(commit, dirty), nodes, edges)


def _extract(files: tuple[str, ...], source_root: str, read_source: SourceReader) -> tuple[tuple[Node, ...], tuple[Edge, ...]]:
    """Nodes, then import, call and inheritance edges, of the given files."""
    nodes = extract_definitions(files, read_source, source_root)
    imports = extract_imports(nodes, read_source)
    calls = extract_calls(nodes, read_source, imports)
    inheritance = extract_inheritance(nodes, read_source, imports)
    return nodes, imports.edges + calls + inheritance
