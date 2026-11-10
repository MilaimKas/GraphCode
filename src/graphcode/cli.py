"""Command line: ``graphcode build-graph`` and ``graphcode view``.

Both read ``[tool.graphcode]`` from the project's ``pyproject.toml`` and
write into ``<project>/.graphcode/``.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from graphcode.config import ConfigError, GraphCodeConfig, load_config
from graphcode.graph.build import build_graph
from graphcode.graph.definitions import ExtractionError
from graphcode.graph.serialization import write_graph
from graphcode.sources import SourceError, working_tree_reader
from graphcode.view.generate import write_viewer

GRAPH_FILE = "graph.json"
VIEWER_FILE = "viewer.html"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command line; return the exit code."""
    parser = _parser()
    options = parser.parse_args(arguments)
    try:
        config = load_config(options.project.resolve())
        if options.command == "build-graph":
            _build(config)
        else:
            _view(config, rebuild=not options.no_build, open_browser=options.open)
    except (ConfigError, SourceError, ExtractionError) as error:
        print(f"graphcode: {error}", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    """Return the argument parser with its subcommands."""
    parser = argparse.ArgumentParser(prog="graphcode", description="View a Python package's architecture.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("build-graph", f"extract the structure graph into .graphcode/{GRAPH_FILE}"),
        ("view", f"write .graphcode/{VIEWER_FILE} (rebuilding the graph first)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project", type=Path, default=Path("."), help="project folder (default: current)")
    view = commands.choices["view"]
    view.add_argument("--no-build", action="store_true", help=f"use the existing {GRAPH_FILE}")
    view.add_argument("--open", action="store_true", help="open the page in the default browser")
    return parser


def _prepare_output_folder(config: GraphCodeConfig) -> Path:
    """Create ``.graphcode/`` with a ``.gitignore`` so its content is not committed."""
    folder = config.output_folder
    folder.mkdir(exist_ok=True)
    ignore_file = folder / ".gitignore"
    if not ignore_file.exists():
        ignore_file.write_text("*\n", encoding="utf-8")
    return folder


def _build(config: GraphCodeConfig) -> Path:
    """Build and write ``graph.json``; print a one-line summary."""
    graph = build_graph(config)
    path = _prepare_output_folder(config) / GRAPH_FILE
    write_graph(graph, path)
    edge_counts = Counter(edge.kind.value for edge in graph.edges)
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(edge_counts.items()))
    print(f"{len(graph.nodes)} nodes, {len(graph.edges)} edges ({summary}) -> {path}")
    return path


def _view(config: GraphCodeConfig, rebuild: bool, open_browser: bool) -> None:
    """Write the viewer page from ``graph.json``, building it first unless told not to."""
    graph_path = config.output_folder / GRAPH_FILE
    if rebuild or not graph_path.exists():
        _build(config)
    page = write_viewer(graph_path, working_tree_reader(config.project_root), config.output_folder / VIEWER_FILE)
    print(f"viewer -> {page}")
    if open_browser:
        webbrowser.open(page.as_uri())
