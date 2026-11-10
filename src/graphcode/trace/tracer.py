"""Record which package functions run, and who called whom, as a tree of steps.

How it works, in plain words
----------------------------
Python 3.12's ``sys.monitoring`` calls our ``on_...`` methods whenever a
function starts, returns, is left by an exception, or (for a generator)
pauses and resumes. We keep a *pile* of the calls in progress: a starting
call goes on top, a finishing call comes off. The call on top when a new one
starts is its parent.

- Only functions defined in the package's files are recorded. For any other
  code we answer ``DISABLE``, and Python stops reporting it: it runs at
  nearly full speed.
- Lambdas, generator expressions and class bodies are not recorded: what they call is
  attached to the named function containing them, as in the static graph.
- Consecutive identical sibling calls (same function, same outcome, same
  sub-calls) collapse into one step with ``count = N``, as soon as they
  finish, so a loop of a million identical calls costs one step.
- Only the thread that starts the trace is recorded; async code is refused.

Example: ``gen.sample(100)`` calling ``noise.sample()`` 100 times gives::

    id  parent  depth  key                              count
    0   None    0      pkg.generator:DataGenerator.sample   1
    1   0       1      pkg.noise:GaussianNoise.sample     100
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from inspect import (
    CO_ASYNC_GENERATOR,
    CO_COROUTINE,
    CO_ITERABLE_COROUTINE,
    CO_NEWLOCALS,
)
from types import CodeType, FrameType
from typing import Any, Literal

from graphcode.keys import GENERATED_QUALNAME_PARTS, MODULE_SEPARATOR

Outcome = Literal["return", "raise", "unfinished"]
"""How a call ended.

``unfinished`` when the trace stopped first (step cap), or for a generator
that was never resumed to its end: Python 3.12 closes a generator abandoned
by ``break`` without reporting it, so "closed" and "still paused" look alike.
"""

DEFAULT_MAX_STEPS = 50_000

# sys.monitoring.PROFILER_ID, written as a number so this module imports on 3.10.
_TOOL_ID = 2
_FOLDED_NAMES = GENERATED_QUALNAME_PARTS - {"<locals>"}  # <lambda>, <genexpr>, comprehensions
_ASYNC_FLAGS = CO_COROUTINE | CO_ASYNC_GENERATOR | CO_ITERABLE_COROUTINE

Signature = tuple[Any, ...]
"""Fingerprint of a finished call and its sub-calls, compared to collapse repeats."""


class TraceError(Exception):
    """Raised when a trace cannot be recorded (old Python, async code, slot taken)."""


@dataclass(frozen=True)
class TraceStep:
    """One step of a trace: a call (or ``count`` identical calls) of a package function.

    Attributes
    ----------
    id, parent : int, int or None
        Position in the step list and the id of the calling step
        (None when called from outside the package, e.g. the scenario).
    depth : int
        Number of package calls above this one.
    key : str
        Join key ``module:qualname`` (just ``module`` for a module body).
    outcome : {"return", "raise", "unfinished"}
        ``raise`` means the call was left by an exception it did not catch.
    exception : str or None
        Name of that exception's type, e.g. ``"ValueError"``.
    count : int
        Number of identical consecutive calls merged into this step.
    matched : bool
        Whether ``key`` is a node of the static graph.
    """

    id: int
    parent: int | None
    depth: int
    key: str
    outcome: Outcome
    exception: str | None
    count: int
    matched: bool


@dataclass(frozen=True)
class RecordedTrace:
    """Result of :func:`record_trace`.

    Attributes
    ----------
    steps : tuple of TraceStep
        Steps in execution order (parents before children).
    truncated : bool
        True when the step cap was reached and recording stopped.
    error : str or None
        ``"Type: message"`` of the exception that ended the traced function, if any.
    """

    steps: tuple[TraceStep, ...]
    truncated: bool
    error: str | None


@dataclass(eq=False)
class _Call:
    """A call being recorded (mutable; becomes a :class:`TraceStep` at the end)."""

    key: str
    # The running copy of the function; dropped when the call ends, so its
    # local variables (possibly large arrays) are not kept alive.
    frame: FrameType | None
    parent: _Call | None
    children: list[_Call] = field(default_factory=list)
    outcome: Outcome = "unfinished"
    exception: str | None = None
    count: int = 1
    signature: Signature | None = None  # set once the call can no longer change


class _Recorder:
    """Receives the monitoring events and builds the call tree.

    Every ``on_...`` method is called by Python itself, from inside the
    monitored function: ``sys._getframe(1)`` is that function's running copy
    (its frame). Matching by frame, not by function, keeps two live copies of
    the same generator apart, e.g. ``zip(batches(x), batches(y))``.
    """

    def __init__(self, module_of_file: Mapping[str, str], max_steps: int) -> None:
        self.module_of_file = module_of_file
        self.max_steps = max_steps
        self.thread = threading.get_ident()
        self.disable = sys.monitoring.DISABLE
        self.roots: list[_Call] = []
        self.open_calls: list[_Call] = []  # the pile: started, not finished
        self.paused: dict[FrameType, _Call] = {}  # generators waiting to resume
        self.key_of_code: dict[CodeType, str | None] = {}
        self.step_count = 0
        self.truncated = False
        self.async_key: str | None = None
        self.stopped = False

    def key_of(self, code: CodeType) -> str | None:
        """Return the join key of ``code``, or None if it is not recorded.

        Not recorded: code outside the package, lambdas and generator
        expressions (folded into their function), and class bodies (running
        a ``class`` statement defines the class, it does not call it).
        """
        if code not in self.key_of_code:
            module = self.module_of_file.get(code.co_filename)
            qualname = code.co_qualname
            is_class_body = not code.co_flags & CO_NEWLOCALS and qualname != "<module>"
            if module is None or is_class_body or qualname.rsplit(".", 1)[-1] in _FOLDED_NAMES:
                self.key_of_code[code] = None
            elif qualname == "<module>":
                self.key_of_code[code] = module
            else:
                self.key_of_code[code] = f"{module}{MODULE_SEPARATOR}{qualname}"
        return self.key_of_code[code]

    def stop(self) -> None:
        """Stop listening; calls still in progress stay ``unfinished``."""
        self.stopped = True
        sys.monitoring.set_events(_TOOL_ID, 0)

    def on_start(self, code: CodeType, offset: int) -> object:
        """A function starts: put a new call on the pile."""
        if threading.get_ident() != self.thread:
            return None
        key = self.key_of(code)
        if key is None:
            return self.disable
        if self.stopped:
            return None
        if code.co_flags & _ASYNC_FLAGS:
            self.async_key = key
            self.stop()
            return None
        if self.step_count >= self.max_steps:
            self.truncated = True
            self.stop()
            return None
        parent = self.open_calls[-1] if self.open_calls else None
        call = _Call(key, sys._getframe(1), parent)
        (parent.children if parent else self.roots).append(call)
        self.open_calls.append(call)
        self.step_count += 1
        return None

    def on_resume(self, code: CodeType, offset: int) -> object:
        """A paused generator continues: its call goes back on the pile."""
        if threading.get_ident() != self.thread or self.stopped:
            return None
        if self.key_of(code) is None:
            return self.disable
        self._resume(sys._getframe(1))
        return None

    def on_throw(self, code: CodeType, offset: int, exception: BaseException) -> object:
        """An exception is sent into a paused generator (``throw``, ``close``): it continues.

        This event cannot be disabled, so it never returns ``DISABLE``.
        """
        if threading.get_ident() != self.thread or self.stopped or self.key_of(code) is None:
            return None
        self._resume(sys._getframe(1))
        return None

    def on_yield(self, code: CodeType, offset: int, value: object) -> object:
        """A generator pauses: its call leaves the pile but stays open."""
        if threading.get_ident() != self.thread or self.stopped:
            return None
        if self.key_of(code) is None:
            return self.disable
        frame = sys._getframe(1)
        call = self._take_top(frame)
        if call is not None:
            self.paused[frame] = call
        return None

    def on_return(self, code: CodeType, offset: int, value: object) -> object:
        """A function returns normally."""
        if threading.get_ident() != self.thread or self.stopped:
            return None
        if self.key_of(code) is None:
            return self.disable
        call = self._take_top(sys._getframe(1))
        if call is not None:
            self._finish(call, "return", None)
        return None

    def on_unwind(self, code: CodeType, offset: int, exception: BaseException) -> object:
        """A function is left by an exception (this event cannot be disabled)."""
        if threading.get_ident() != self.thread or self.stopped or self.key_of(code) is None:
            return None
        call = self._take_top(sys._getframe(1))
        if call is not None:
            if isinstance(exception, GeneratorExit):  # a generator closed early: a normal end
                self._finish(call, "return", None)
            else:
                self._finish(call, "raise", type(exception).__name__)
        return None

    def _resume(self, frame: FrameType) -> None:
        """Put the paused call of ``frame`` back on the pile, if there is one."""
        call = self.paused.pop(frame, None)
        if call is not None:
            self.open_calls.append(call)

    def _take_top(self, frame: FrameType) -> _Call | None:
        """Remove the top call of the pile if it belongs to ``frame``."""
        if self.open_calls and self.open_calls[-1].frame is frame:
            return self.open_calls.pop()
        return None

    def _finish(self, call: _Call, outcome: Outcome, exception: str | None) -> None:
        """Record how ``call`` ended; merge it into the previous sibling if identical."""
        call.outcome, call.exception = outcome, exception
        self._seal(call)
        siblings = call.parent.children if call.parent else self.roots
        if len(siblings) >= 2 and siblings[-1] is call and siblings[-2].signature == call.signature:
            siblings.pop()
            siblings[-1].count += 1
            self.step_count -= _size(call)

    def _seal(self, call: _Call) -> None:
        """Freeze ``call``: compute its signature and forget its frame.

        A sub-call still open at this point is a generator its caller has
        abandoned (e.g. left by ``break``; Python 3.12 closes it without an
        event). It is sealed too, as ``unfinished``, so it does not stop its
        callers from being merged.
        """
        for child in call.children:
            if child.signature is None:
                self._seal(child)
        if call.frame is not None:
            self.paused.pop(call.frame, None)
            call.frame = None
        call.signature = (
            call.key, call.outcome, call.exception, tuple((child.signature, child.count) for child in call.children)
        )


def _size(call: _Call) -> int:
    """Number of calls in the subtree of ``call``, itself included."""
    total, pending = 0, [call]
    while pending:
        current = pending.pop()
        total += 1
        pending.extend(current.children)
    return total


def _flatten(roots: Sequence[_Call], known_keys: AbstractSet[str]) -> tuple[TraceStep, ...]:
    """Number the calls depth first (parents before children) and freeze them."""
    steps: list[TraceStep] = []
    # A to-do list instead of recursion: deep recursive code must not hit Python's limit.
    pending: list[tuple[_Call, int | None, int]] = [(root, None, 0) for root in reversed(roots)]
    while pending:
        call, parent_id, depth = pending.pop()
        step_id = len(steps)
        matched = call.key in known_keys
        steps.append(
            TraceStep(step_id, parent_id, depth, call.key, call.outcome, call.exception, call.count, matched)
        )
        pending.extend((child, step_id, depth + 1) for child in reversed(call.children))
    return tuple(steps)


def record_trace(
    function: Callable[..., Any],
    args: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
    *,
    module_of_file: Mapping[str, str],
    known_keys: AbstractSet[str],
    max_steps: int = DEFAULT_MAX_STEPS,
) -> RecordedTrace:
    """Call ``function(*args, **kwargs)`` and record the package calls it makes.

    Parameters
    ----------
    function : callable
        What to run; it may be inside or outside the package (a scenario).
    args, kwargs
        Its arguments.
    module_of_file : mapping of str to str
        Absolute path of each package file (as Python reports it in
        ``co_filename``) to its dotted module name. Other files are ignored.
    known_keys : set of str
        Keys of the static graph's nodes, to set ``matched``.
    max_steps : int
        Recording stops (``truncated``) when this many distinct steps exist.

    Returns
    -------
    RecordedTrace
        The steps; an exception raised by ``function`` is caught and named
        in ``error``, so the steps are never lost. ``SystemExit`` counts as
        an error only with a non-zero code.

    Raises
    ------
    TraceError
        Before Python 3.12, when the monitoring slot is taken, or when the
        traced code runs an async function.
    """
    if sys.version_info < (3, 12):
        raise TraceError("tracing needs Python 3.12 or later (sys.monitoring)")
    monitoring = sys.monitoring
    try:
        monitoring.use_tool_id(_TOOL_ID, "graphcode")
    except ValueError as error:
        raise TraceError("another tool (a profiler?) is already using sys.monitoring") from error
    recorder = _Recorder(module_of_file, max_steps)
    events = monitoring.events
    callbacks = {
        events.PY_START: recorder.on_start,
        events.PY_RESUME: recorder.on_resume,
        events.PY_THROW: recorder.on_throw,
        events.PY_YIELD: recorder.on_yield,
        events.PY_RETURN: recorder.on_return,
        events.PY_UNWIND: recorder.on_unwind,
    }
    error_text = None
    try:
        for event, callback in callbacks.items():
            monitoring.register_callback(_TOOL_ID, event, callback)
        monitoring.set_events(_TOOL_ID, sum(callbacks))  # event constants are distinct bit flags
        try:
            function(*args, **(kwargs or {}))
        except SystemExit as raised:
            if raised.code not in (None, 0):  # sys.exit() or sys.exit(0) is a normal end of a "main"
                error_text = f"SystemExit: {raised.code}"
        except Exception as raised:  # noqa: BLE001 - any failure of the traced code is a result
            error_text = f"{type(raised).__name__}: {raised}"
    finally:
        monitoring.set_events(_TOOL_ID, 0)
        for event in callbacks:
            monitoring.register_callback(_TOOL_ID, event, None)
        monitoring.free_tool_id(_TOOL_ID)
        # Undo every DISABLE (of all tools: others simply disable again), so the next trace starts clean.
        monitoring.restart_events()
    if recorder.async_key is not None:
        raise TraceError(f"async code is not supported (met {recorder.async_key})")
    return RecordedTrace(_flatten(recorder.roots, known_keys), recorder.truncated, error_text)
