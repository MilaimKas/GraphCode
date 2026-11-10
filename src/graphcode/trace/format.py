"""Reading and writing trace files (``.graphcode/traces/<scenario>.json``).

Reading checks the structure (version, fields, the tree of steps), so a
damaged file fails here with one error type, :class:`TraceFormatError`.
Field types are not checked one by one: the files are written by graphcode.

File layout (schema version 1)::

    {
      "header": {"schema_version": 1, "scenario": "sample-small",
                 "target": "scripts.scenarios:sample_small",
                 "date": "2026-11-08T21:14:03", "commit": "a1b2c3d", "dirty": false,
                 "python_version": "3.12.7", "graphcode_version": "0.1.0",
                 "step_count": 2, "truncated": false, "error": null},
      "steps": [{"id": 0, "parent": null, "depth": 0, "key": "pkg.mod:f",
                 "outcome": "return", "exception": null, "count": 1,
                 "matched": true, "args": null, "return_value": null}]
    }

``error`` names the exception that ended the traced function, if any.
``args`` and ``return_value`` are reserved for variable tracking (always null).
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, fields
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, get_args

from graphcode.trace.tracer import Outcome, RecordedTrace, TraceStep

SCHEMA_VERSION = 1


class TraceFormatError(Exception):
    """Raised when a trace file is not valid JSON or not a valid trace."""


@dataclass(frozen=True)
class TraceHeader:
    """Provenance of a trace: what was run, on which code, by which Python.

    Attributes
    ----------
    schema_version : int
        Version of the file format.
    scenario, target : str
        Name of the scenario and what was run (``module:qualname``).
    date : str
        When it was recorded, ISO format to the second.
    commit, dirty
        Git commit of the traced code (None outside git) and whether the
        working tree had uncommitted changes.
    python_version, graphcode_version : str
        Versions that recorded the trace.
    step_count : int
        Number of steps in the file.
    truncated : bool
        True when the step cap stopped the recording.
    error : str or None
        ``"Type: message"`` of the exception that ended the traced function.
    """

    schema_version: int
    scenario: str
    target: str
    date: str
    commit: str | None
    dirty: bool
    python_version: str
    graphcode_version: str
    step_count: int
    truncated: bool
    error: str | None


@dataclass(frozen=True)
class TraceFile:
    """The full content of one trace file."""

    header: TraceHeader
    steps: tuple[TraceStep, ...]


def trace_file_for(
    recorded: RecordedTrace, scenario: str, target: str, commit: str | None, dirty: bool
) -> TraceFile:
    """Wrap a fresh recording with a header describing it (date: now)."""
    header = TraceHeader(
        schema_version=SCHEMA_VERSION,
        scenario=scenario,
        target=target,
        date=datetime.now().isoformat(timespec="seconds"),
        commit=commit,
        dirty=dirty,
        python_version=platform.python_version(),
        graphcode_version=_installed_graphcode_version(),
        step_count=len(recorded.steps),
        truncated=recorded.truncated,
        error=recorded.error,
    )
    return TraceFile(header, recorded.steps)


def trace_to_dict(trace: TraceFile) -> dict[str, Any]:
    """Convert a trace to plain JSON data."""
    header = trace.header
    return {
        "header": {field.name: getattr(header, field.name) for field in fields(TraceHeader)},
        "steps": [
            {
                "id": step.id,
                "parent": step.parent,
                "depth": step.depth,
                "key": step.key,
                "outcome": step.outcome,
                "exception": step.exception,
                "count": step.count,
                "matched": step.matched,
                "args": None,
                "return_value": None,
            }
            for step in trace.steps
        ],
    }


def trace_from_dict(data: dict[str, Any]) -> TraceFile:
    """Build a trace from JSON data written by :func:`trace_to_dict`.

    Raises
    ------
    TraceFormatError
        If the schema version differs, a field is missing or has a wrong
        value, or the steps do not form a tree in execution order.
    """
    try:
        header_data = data["header"]
        if header_data["schema_version"] != SCHEMA_VERSION:
            raise TraceFormatError(
                f"trace has schema version {header_data['schema_version']}, "
                f"this graphcode reads version {SCHEMA_VERSION}; record it again"
            )
        header = TraceHeader(**{field.name: header_data[field.name] for field in fields(TraceHeader)})
        if not isinstance(data["steps"], list):
            raise TraceFormatError("trace steps must be a list")
        steps = tuple(_step_from_dict(step_data) for step_data in data["steps"])
        if header.step_count != len(steps):
            raise TraceFormatError(f"header says {header.step_count} steps, the file has {len(steps)}")
        _check_tree(steps)
    except TraceFormatError:
        raise
    # Missing fields (KeyError) and wrong types, e.g. a parent "0" (TypeError,
    # AttributeError),
    # become one error type, so callers only handle TraceFormatError.
    except (KeyError, TypeError, AttributeError) as error:
        raise TraceFormatError(f"invalid trace content: {error!r}") from error
    return TraceFile(header, steps)


def _step_from_dict(step_data: dict[str, Any]) -> TraceStep:
    """Build one step, checking its outcome is a known value."""
    if step_data["outcome"] not in get_args(Outcome):
        raise TraceFormatError(f"unknown outcome {step_data['outcome']!r} in step {step_data['id']}")
    return TraceStep(
        id=step_data["id"],
        parent=step_data["parent"],
        depth=step_data["depth"],
        key=step_data["key"],
        outcome=step_data["outcome"],
        exception=step_data["exception"],
        count=step_data["count"],
        matched=step_data["matched"],
    )


def _check_tree(steps: tuple[TraceStep, ...]) -> None:
    """Check ids are 0, 1, 2...; each parent comes earlier and is one level up.

    Steps written by the tracer are in execution order (depth first); this
    check does not verify that order beyond "parent before child".
    """
    for position, step in enumerate(steps):
        if step.id != position:
            raise TraceFormatError(f"step at position {position} has id {step.id}")
        if step.parent is None:
            expected_depth = 0
        elif 0 <= step.parent < position:
            expected_depth = steps[step.parent].depth + 1
        else:
            raise TraceFormatError(f"step {step.id} has parent {step.parent}, not an earlier step")
        if step.depth != expected_depth:
            raise TraceFormatError(f"step {step.id} has depth {step.depth}, expected {expected_depth}")


def write_trace(trace: TraceFile, path: Path) -> None:
    """Write a trace to ``path`` as UTF-8 JSON, creating parent folders.

    One step per line: readable, and a large trace stays a manageable file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = trace_to_dict(trace)
    # Default ASCII escaping: an exception message in "error" may hold characters
    # (e.g. from undecodable file names) that UTF-8 cannot encode.
    lines = ",\n".join(f"    {json.dumps(step)}" for step in data["steps"])
    header = json.dumps(data["header"])
    steps_block = f"[\n{lines}\n  ]" if lines else "[]"
    path.write_text(f'{{\n  "header": {header},\n  "steps": {steps_block}\n}}\n', encoding="utf-8")


def read_trace(path: Path) -> TraceFile:
    """Read and validate a trace written by :func:`write_trace`.

    Raises
    ------
    TraceFormatError
        If the file is not valid JSON or not a valid trace.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TraceFormatError(f"{path} is not valid UTF-8 JSON: {error}") from error
    return trace_from_dict(data)


def _installed_graphcode_version() -> str:
    """Return the installed graphcode version, or ``"unknown"`` if not installed.

    Repeated from ``graph.model`` on purpose: ``trace`` never imports ``graph``.
    """
    try:
        return version("graphcode")
    except PackageNotFoundError:
        return "unknown"
