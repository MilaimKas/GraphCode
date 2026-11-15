"""Command line: ``graphcode build-graph``, ``view``, ``trace`` and ``diff``.

All read ``[tool.graphcode]`` from the project's ``pyproject.toml`` and
write into ``<project>/.graphcode/``. Examples of ``trace``::

    graphcode trace datagenerator.dag:load --args '["x.json"]'
    graphcode trace datagenerator.cli:main -- --rows 10
    graphcode trace @sample-small
    graphcode trace --all

and of ``diff``::

    graphcode diff main              # main vs the working tree
    graphcode diff main..feature-x   # the last commits of both
    graphcode diff main...feature-x  # what feature-x changed since it left main (as a PR)
"""

from __future__ import annotations

import argparse
import platform
import re
import subprocess
import sys
import webbrowser
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from graphcode.config import ConfigError, GraphCodeConfig, load_config
from graphcode.diff.compare import IncomparableGraphsError, compare_graphs
from graphcode.diff.load import base_ref, graph_at, parse_range, reader_at
from graphcode.diff.page import diff_page_data
from graphcode.diff.report import markdown_report, version_label
from graphcode.graph.build import build_graph
from graphcode.graph.definitions import ExtractionError
from graphcode.graph.serialization import write_graph
from graphcode.sources import SourceError, working_tree_reader
from graphcode.trace.runner import TRACES_FOLDER, trace_in_child
from graphcode.trace.targets import (
    SCENARIO_PREFIX,
    TargetError,
    TraceTarget,
    all_scenario_targets,
    parse_target,
    scenario_target,
)
from graphcode.view.generate import write_diff_page, write_viewer

GRAPH_FILE = "graph.json"
VIEWER_FILE = "viewer.html"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command line; return the exit code."""
    words = list(sys.argv[1:] if arguments is None else arguments)
    # Words after "--" belong to the traced entry point, not to graphcode.
    target_argv = words[words.index("--") + 1 :] if "--" in words else None
    if target_argv is not None:
        words = words[: words.index("--")]
    parser = _parser()
    options = parser.parse_args(words)
    try:
        config = load_config(options.project.resolve())
        if options.command == "build-graph":
            _build(config)
        elif options.command == "view":
            _view(config, rebuild=not options.no_build, open_browser=options.open)
        elif options.command == "diff":
            _diff(config, options, parser)
        else:
            return _trace(config, _trace_targets(config, options, target_argv, parser), rebuild=not options.no_build)
    except (ConfigError, SourceError, ExtractionError, TargetError, IncomparableGraphsError) as error:
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
        ("trace", "record which package functions a call runs, into .graphcode/traces/"),
        ("diff", "show how the graph changes between two versions (refs read from git)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project", type=Path, default=Path("."), help="project folder (default: current)")
    view = commands.choices["view"]
    view.add_argument("--no-build", action="store_true", help=f"use the existing {GRAPH_FILE}")
    view.add_argument("--open", action="store_true", help="open the page in the default browser")
    trace = commands.choices["trace"]
    trace.add_argument("target", nargs="?", help=f"module:function, or {SCENARIO_PREFIX}scenario from pyproject.toml")
    trace.add_argument("--args", help="positional arguments as a JSON list, e.g. '[3, \"x\"]'")
    trace.add_argument("--kwargs", help="keyword arguments as a JSON object, e.g. '{\"rows\": 10}'")
    trace.add_argument("--name", help="trace name (file traces/<name>.json); default: the function's name")
    trace.add_argument("--all", action="store_true", help="trace every scenario of pyproject.toml")
    trace.add_argument("--no-build", action="store_true", help=f"use the existing {GRAPH_FILE}")
    trace.epilog = "Words after '--' are passed to the traced function as its command line (sys.argv)."
    diff = commands.choices["diff"]
    diff.add_argument(
        "range", help="<ref> (vs the working tree), <ref>..<ref> (both last commits) or <ref>...<ref> (since the branch point)"
    )
    diff.add_argument("--no-cache", action="store_true", help="rebuild commit graphs instead of reusing .graphcode/cache/")
    diff.add_argument(
        "--format", choices=["html", "md"], default="html",
        help="html: write .graphcode/diff-<base>-<head>.html (default); md: print a Markdown summary",
    )
    diff.add_argument("--open", action="store_true", help="open the diff page in the browser")
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
    page = write_viewer(
        graph_path,
        working_tree_reader(config.project_root),
        config.output_folder / VIEWER_FILE,
        config.output_folder / TRACES_FOLDER,
    )
    print(f"viewer -> {page}")
    if open_browser:
        _open_in_browser(page)


def _trace_targets(
    config: GraphCodeConfig,
    options: argparse.Namespace,
    target_argv: list[str] | None,
    parser: argparse.ArgumentParser,
) -> list[TraceTarget]:
    """Turn the ``trace`` options into the list of targets to run."""
    if options.all:
        if options.target or options.args or options.kwargs or options.name or target_argv is not None:
            parser.error("trace --all takes no target, arguments or name")
        targets = all_scenario_targets(config.scenarios)
        if not targets:
            raise TargetError("trace --all: no scenarios in [tool.graphcode.scenarios]")
        return targets
    if not options.target:
        parser.error(f"trace needs a target (module:function or {SCENARIO_PREFIX}scenario), or --all")
    if options.target.startswith(SCENARIO_PREFIX):
        if options.args or options.kwargs or target_argv is not None:
            parser.error("a scenario's arguments are set in pyproject.toml, not on the command line")
        return [scenario_target(config.scenarios, options.target)]
    return [parse_target(options.target, options.args, options.kwargs, target_argv, options.name)]


def _trace(config: GraphCodeConfig, targets: list[TraceTarget], rebuild: bool) -> int:
    """Trace each target in its own Python process; return the worst exit code.

    The graph is rebuilt first, so each step is matched against the current code.
    """
    if rebuild or not (config.output_folder / GRAPH_FILE).exists():
        _build(config)
    exit_codes = [trace_in_child(config.project_root, target) for target in targets]
    return max(exit_codes)


def _diff(config: GraphCodeConfig, options: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Show how the graph changes from the first version to the second (or the working tree)."""
    try:
        versions = parse_range(options.range)
    except ValueError as error:
        parser.error(str(error))
    _prepare_output_folder(config)  # the commit graph cache lives in .graphcode/cache/
    before = base_ref(config, versions)
    base = graph_at(config, before, not options.no_cache)
    head = graph_at(config, versions.head, not options.no_cache)
    diff = compare_graphs(base, head)
    base_name = f"where {versions.head} left {versions.base}" if versions.from_branch_point else versions.base
    if options.format == "md":
        print(markdown_report(diff, base_name, versions.head), end="")
        return
    graph, sources, details = diff_page_data(
        diff, base, head, reader_at(config, before), reader_at(config, versions.head)
    )
    details |= {"base_label": version_label(base_name, base.header), "head_label": version_label(versions.head, head.header)}
    file_name = f"diff-{_file_safe(versions.base)}-{_file_safe(versions.head or 'worktree')}.html"
    page = write_diff_page(graph, sources, details, config.output_folder / file_name)
    print(f"{len(diff.added)} added, {len(diff.removed)} removed, {len(diff.modified)} modified, {len(diff.moved)} moved -> {page}")
    if options.open:
        _open_in_browser(page)


def _file_safe(ref: str) -> str:
    """A ref usable in a file name: "feature/x" -> "feature-x", "HEAD~2" -> "HEAD-2"."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ref)


def _open_in_browser(page: Path) -> None:
    """Open the page in the default browser; under WSL, in the Windows one.

    Python's ``webbrowser`` finds no browser inside WSL, so the page is
    handed to ``explorer.exe`` with its Windows path.
    """
    if "microsoft" in platform.release().lower():
        windows_path = subprocess.run(["wslpath", "-w", str(page)], capture_output=True, text=True, check=True).stdout.strip()
        # explorer.exe returns exit code 1 even on success: do not check it.
        subprocess.run(["explorer.exe", windows_path], check=False)
    else:
        webbrowser.open(page.as_uri())
