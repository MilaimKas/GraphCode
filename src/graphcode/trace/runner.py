"""Run one trace in a fresh Python process, and write its trace file.

Two halves of one hand-over:

- :func:`trace_in_child` (in the ``graphcode`` command) starts
  ``python -m graphcode.trace.runner <project> <target as JSON> <package> <output>``
  with the same interpreter, in the project folder (``<package>`` is the
  package to graph and ``<output>`` the ``--output`` folder; empty means "as in
  pyproject.toml" and ``<project>/.graphcode/``);
- :func:`main` (in that new process) records the call and writes
  ``<output folder>/traces/<name>.json``.

A fresh process per trace means every scenario starts from the same clean
state, and a crash or ``sys.exit`` in the scenario cannot hurt ``graphcode``.
The interpreter must be the analysed project's (``uv run graphcode ...`` in
that project), since the scenario imports the project's own libraries.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from graphcode.config import ConfigError, load_config
from graphcode.keys import module_name_from_path
from graphcode.sources import SourceError, find_package_files, git_state
from graphcode.trace.format import trace_file_for, write_trace
from graphcode.trace.targets import TargetError, TraceTarget, prepare_call
from graphcode.trace.tracer import TraceError, TraceStep, record_trace

GRAPH_FILE = "graph.json"  # must match graphcode.cli.GRAPH_FILE (trace does not import cli)
TRACES_FOLDER = "traces"


def trace_in_child(
    project_root: Path, target: TraceTarget, package: str | None = None, output_folder: Path | None = None
) -> int:
    """Trace ``target`` in a new Python process; return its exit code (0: trace written).

    Parameters
    ----------
    project_root : Path
        Folder of the analysed project; the new process runs there.
    target : TraceTarget
        The call to record.
    package : str or None
        The package to graph, passed on to the new process; None: as in pyproject.toml.
    output_folder : Path or None
        The ``--output`` override; made absolute here, since the new process
        runs in another folder.

    Returns
    -------
    int
        The new process's exit code, or 1 if it was killed by a signal.
    """
    project_root = project_root.resolve()
    command = [
        sys.executable,
        "-m",
        "graphcode.trace.runner",
        str(project_root),
        _target_to_json(target),
        package or "",  # an empty word means "no override"
        "" if output_folder is None else str(output_folder.resolve()),
    ]
    # The child's output (the scenario's prints, then the summary) goes straight to the terminal.
    process = subprocess.Popen(command, cwd=project_root)
    try:
        return_code = process.wait()
    except KeyboardInterrupt:
        # Ctrl-C reaches the child too: wait while it writes the partial trace.
        return_code = process.wait()
    if return_code < 0:  # killed by a signal (e.g. a crash inside C code): no finally ran
        print(f"graphcode trace: the scenario's process was killed (signal {-return_code}); no trace written")
        return 1
    return return_code


def main(arguments: Sequence[str] | None = None) -> int:
    """Entry point of the new process: ``<project root> <target as JSON> <package> <output>``.

    Parameters
    ----------
    arguments : sequence of str or None
        The four words; None reads them from ``sys.argv``. An empty
        ``<package>`` or ``<output>`` means "no override".

    Returns
    -------
    int
        0 if the trace was written, 1 if the configuration or target is invalid.
    """
    project_text, target_text, package_text, output_text = sys.argv[1:] if arguments is None else arguments
    try:
        path, summary = run_trace(
            Path(project_text),
            _target_from_json(target_text),
            package=package_text or None,
            output_folder=Path(output_text) if output_text else None,
        )
    except (ConfigError, SourceError, TargetError, TraceError) as error:
        print(f"graphcode trace: {error}", file=sys.stderr)
        return 1
    print(f"{summary} -> {path}")
    return 0


def run_trace(
    project_root: Path, target: TraceTarget, package: str | None = None, output_folder: Path | None = None
) -> tuple[Path, str]:
    """Record ``target`` in this process and write its trace file.

    Parameters
    ----------
    project_root : Path
        Folder of the analysed project.
    target : TraceTarget
        The call to record.
    package : str or None
        The ``--package`` override (see ``config.load_config``).
    output_folder : Path or None
        The ``--output`` override (see ``config.load_config``).

    Returns
    -------
    tuple of (Path, str)
        Where the trace was written, and a one-line summary of it.

    Raises
    ------
    ConfigError, SourceError
        If the project's configuration or package is invalid.
    TargetError
        If the target cannot be imported or found (nothing has run yet).
    TraceError
        If tracing is impossible (Python before 3.12, async code).
    """
    project_root = project_root.resolve()
    config = load_config(project_root, package, output_folder)
    layout = find_package_files(project_root, config.package, config.exclude)
    source_root = project_root / layout.source_root
    # Imports must find the package (src/ layout) and scenario scripts (project
    # root), in front of any installed copy, so its files are the ones listed below.
    for folder in (str(project_root), str(source_root)):
        while folder in sys.path:
            sys.path.remove(folder)
        sys.path.insert(0, folder)
    module_of_file = {
        str(project_root / file): module_name_from_path((project_root / file).relative_to(source_root).as_posix())
        for file in layout.files
    }
    known_keys, graph_note = _graph_keys(config.output_folder / GRAPH_FILE)
    call = prepare_call(target)
    package_file = getattr(sys.modules.get(config.package), "__file__", None)
    if package_file is not None and package_file not in module_of_file:
        graph_note += f"; warning: {config.package} was imported from {package_file}, not the project: nothing recorded"
    recorded = record_trace(call, module_of_file=module_of_file, known_keys=known_keys)
    commit, dirty = git_state(project_root, [layout.source_root or config.package])
    path = config.output_folder / TRACES_FOLDER / f"{target.name}.json"
    write_trace(trace_file_for(recorded, target.name, target.reference, commit, dirty), path)
    return path, _summary(recorded.steps, recorded.truncated, recorded.error, graph_note)


def _graph_keys(graph_path: Path) -> tuple[frozenset[str], str]:
    """Return the node keys of ``graph.json`` and a note naming it if it is missing or unreadable.

    Read as plain JSON rather than through ``graph``: ``trace`` never imports ``graph``.
    Without a graph the trace is still recorded, its steps simply not matched.
    """
    if not graph_path.is_file():
        return frozenset(), f"; no {graph_path}: steps not matched to the graph"
    try:
        nodes = json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
        return frozenset(node["key"] for node in nodes), ""
    except (OSError, ValueError, KeyError, TypeError) as error:  # ValueError covers bad JSON and UTF-8
        return frozenset(), f"; {graph_path} unreadable ({error!r}): steps not matched"


def _summary(steps: Sequence[TraceStep], truncated: bool, error: str | None, graph_note: str) -> str:
    """One line such as ``"12 steps (3 not in the graph); the scenario raised ValueError: x"``."""
    unmatched = sum(1 for step in steps if not step.matched)
    text = f"{len(steps)} steps"
    if unmatched and not graph_note:
        text += f" ({unmatched} not in the graph)"
    if truncated:
        text += ", truncated at the step cap"
    if error is not None:
        text += f"; the scenario raised {error}"
    return text + graph_note


def _target_to_json(target: TraceTarget) -> str:
    """Encode a target for the command line of the new process."""
    return json.dumps(
        {
            "name": target.name,
            "reference": target.reference,
            "args": list(target.args),
            "kwargs": target.kwargs,
            "argv": None if target.argv is None else list(target.argv),
        }
    )


def _target_from_json(text: str) -> TraceTarget:
    """Decode a target written by :func:`_target_to_json`."""
    data = json.loads(text)
    argv = data["argv"]
    return TraceTarget(
        data["name"], data["reference"], tuple(data["args"]), data["kwargs"], None if argv is None else tuple(argv)
    )


if __name__ == "__main__":
    raise SystemExit(main())
