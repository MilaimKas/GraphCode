"""Build the whole structure graph of a configured project: stage 1 end to end."""

from __future__ import annotations

from graphcode.config import GraphCodeConfig
from graphcode.graph.calls import extract_calls
from graphcode.graph.definitions import extract_definitions
from graphcode.graph.imports import extract_imports
from graphcode.graph.inheritance import extract_inheritance
from graphcode.graph.model import ArchitectureGraph, GraphHeader
from graphcode.sources import find_package_files, git_state, working_tree_reader


def build_graph(config: GraphCodeConfig) -> ArchitectureGraph:
    """Extract nodes and import, call and inheritance edges from the working tree.

    Parameters
    ----------
    config : GraphCodeConfig
        Project root, package name and excluded paths.

    Returns
    -------
    ArchitectureGraph
        The validated graph; its header records the git commit and whether
        the package's files have uncommitted changes.
    """
    layout = find_package_files(config.project_root, config.package, config.exclude)
    read_source = working_tree_reader(config.project_root)
    nodes = extract_definitions(layout.files, read_source, layout.source_root)
    imports = extract_imports(nodes, read_source)
    calls = extract_calls(nodes, read_source, imports)
    inheritance = extract_inheritance(nodes, read_source, imports)
    package_folder = f"{layout.source_root}/{config.package}".lstrip("/")
    commit, dirty = git_state(config.project_root, [package_folder])
    return ArchitectureGraph(GraphHeader.for_current_build(commit, dirty), nodes, imports.edges + calls + inheritance)
