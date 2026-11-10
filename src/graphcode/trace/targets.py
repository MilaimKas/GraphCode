"""What to trace: turn a command-line target or a named scenario into a call.

Three ways to say what to run, all ending as a :class:`TraceTarget`::

    graphcode trace datagenerator.dag:load --args '["x.json"]'   # function + arguments
    graphcode trace datagenerator.cli:main -- --rows 10           # command-line entry point
    graphcode trace @sample-small                                 # named in pyproject.toml

Named scenarios, in ``[tool.graphcode.scenarios]``, use the same syntax::

    sample-small = "scripts.scenarios:sample_small"
    load = { target = "datagenerator.dag:load", args = ["x.json"] }
    cli = { target = "datagenerator.cli:main", argv = ["--rows", "10"] }

An instance method cannot be a target (it needs an object): write a scenario
function that builds the object and makes the call.
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from graphcode.keys import InvalidKeyError, NodeKey

SCENARIO_PREFIX = "@"
SCENARIO_FIELDS = frozenset({"target", "args", "kwargs", "argv"})


class TargetError(Exception):
    """Raised when a target or scenario is malformed or cannot be found."""


@dataclass(frozen=True)
class TraceTarget:
    """A function to run, its arguments, and the name of the trace.

    Attributes
    ----------
    name : str
        Scenario name; the trace is saved as ``traces/<name>.json``.
    reference : str
        ``module:qualname`` of the function, e.g. ``"scripts.scenarios:sample_small"``.
    args : tuple
        Positional arguments (JSON values).
    kwargs : dict
        Keyword arguments (JSON values).
    argv : tuple of str or None
        For a command-line entry point: the words after its name, put in
        ``sys.argv`` while it runs. None for a plain function call.
    """

    name: str
    reference: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    argv: tuple[str, ...] | None = None


def parse_target(
    text: str,
    args_json: str | None = None,
    kwargs_json: str | None = None,
    argv: Sequence[str] | None = None,
    name: str | None = None,
) -> TraceTarget:
    """Build a target from the command line's words.

    The trace name defaults to the qualified name, e.g. ``"DAG-from_file"``
    for ``pkg.dag:DAG.from_file``.

    Raises
    ------
    TargetError
        If ``text`` is not ``module:qualname``, the arguments are not a
        JSON list / object, or ``name`` cannot be a file name.
    """
    reference = _checked_reference(text)
    args, kwargs = [], {}
    if args_json:
        args = _parse_json(args_json, list, "--args must be a JSON list, e.g. '[3, \"x\"]'")
    if kwargs_json:
        kwargs = _parse_json(kwargs_json, dict, "--kwargs must be a JSON object, e.g. '{\"rows\": 10}'")
    trace_name = _checked_name(name) if name else reference.partition(":")[2].replace(".", "-")
    return TraceTarget(trace_name, reference, tuple(args), kwargs, None if argv is None else tuple(argv))


def scenario_target(scenarios: Mapping[str, Any], text: str) -> TraceTarget:
    """Return the scenario named ``text`` (with or without the leading ``@``).

    Raises
    ------
    TargetError
        If no scenario has that name, or its entry is malformed.
    """
    name = text.removeprefix(SCENARIO_PREFIX)
    if name not in scenarios:
        known = ", ".join(sorted(scenarios)) or "none configured"
        raise TargetError(f"no scenario {name!r} in [tool.graphcode.scenarios] (known: {known})")
    _checked_name(name)
    entry = scenarios[name]
    if isinstance(entry, str):
        return TraceTarget(name, _checked_reference(entry))
    if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
        raise TargetError(f'scenario {name!r} must be "module:qualname" or a table with a "target"')
    unknown = set(entry) - SCENARIO_FIELDS
    if unknown:
        raise TargetError(f"scenario {name!r}: unknown field(s) {sorted(unknown)}; use {sorted(SCENARIO_FIELDS)}")
    args, kwargs, argv = entry.get("args", []), entry.get("kwargs", {}), entry.get("argv")
    if not isinstance(args, list) or not isinstance(kwargs, dict) or not isinstance(argv, list | None):
        raise TargetError(f"scenario {name!r}: args and argv must be lists, kwargs a table")
    return TraceTarget(
        name, _checked_reference(entry["target"]), tuple(args), kwargs, None if argv is None else tuple(map(str, argv))
    )


def all_scenario_targets(scenarios: Mapping[str, Any]) -> list[TraceTarget]:
    """Return every configured scenario, sorted by name (for ``trace --all``)."""
    return [scenario_target(scenarios, name) for name in sorted(scenarios)]


def prepare_call(target: TraceTarget) -> Callable[[], Any]:
    """Import the target's function now, and return an argument-free call of it.

    Importing first means module bodies run *before* tracing starts, so the
    trace shows only what the call itself does.

    Raises
    ------
    TargetError
        If the module cannot be imported, has no such function, or the
        function is an instance method (it would run with a wrong ``self``).
    """
    module_name, _, qualname = target.reference.partition(":")
    try:
        function: Any = importlib.import_module(module_name)
    except (ImportError, SyntaxError) as error:
        raise TargetError(f"cannot import {module_name!r}: {error}") from error
    owner_name = module_name
    for part in qualname.split("."):
        if not hasattr(function, part):
            raise TargetError(f"{target.reference}: {owner_name} has no {part!r}")
        # Looked up without binding: a plain function inside a class is an
        # instance method; static and class methods are wrapped, so they pass.
        if inspect.isclass(function) and inspect.isfunction(inspect.getattr_static(function, part)):
            raise TargetError(
                f"{target.reference} is an instance method; write a scenario function that builds the object"
            )
        function, owner_name = getattr(function, part), part
    if not callable(function):
        raise TargetError(f"{target.reference} is not a function or class")

    def call() -> Any:
        """Run the target; for an entry point, with ``sys.argv`` set as on a command line."""
        if target.argv is None:
            return function(*target.args, **target.kwargs)
        saved_argv = sys.argv
        sys.argv = [target.reference, *target.argv]
        try:
            return function(*target.args, **target.kwargs)
        finally:
            sys.argv = saved_argv

    return call


def _checked_reference(text: str) -> str:
    """Return ``text`` if it is ``module:qualname`` with a plain qualname, else raise."""
    try:
        key = NodeKey.from_string(text)
    except InvalidKeyError as error:
        raise TargetError(f"invalid target {text!r}: {error}") from error
    if key.is_module:
        raise TargetError(f"target {text!r} names a module; add ':function', e.g. '{text}:main'")
    if not all(part.isidentifier() for part in key.qualname.split(".")):
        raise TargetError(f"target {text!r}: a nested function (<locals>) cannot be reached from outside")
    return text


def _checked_name(name: str) -> str:
    """Return ``name`` if it can be a trace file name (no folders, no leading dot)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise TargetError(f"trace name {name!r} must be a plain file name (no '/', no leading '.')")
    return name


def _parse_json(text: str, expected_type: type, message: str) -> Any:
    """Parse ``text`` as JSON of ``expected_type``, or raise ``TargetError(message)``."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise TargetError(f"{message} ({error})") from error
    if not isinstance(value, expected_type):
        raise TargetError(message)
    return value
