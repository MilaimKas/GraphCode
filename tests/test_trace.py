"""Tests for the tracer (real runs on the fixture and synthetic cases) and trace files."""

from __future__ import annotations

import importlib
import json
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from graphcode.keys import module_name_from_path
from graphcode.sources import find_package_files
from graphcode.trace.format import (
    TraceFile,
    TraceFormatError,
    read_trace,
    trace_file_for,
    trace_from_dict,
    trace_to_dict,
    write_trace,
)
from graphcode.trace.tracer import RecordedTrace, TraceError, TraceStep, record_trace

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

FIXTURE_ROOT = (Path(__file__).parent / "fixtures" / "sample_project").resolve()

CASES_SOURCE = '''
import asyncio
import threading

def leaf(value):
    return value

def fail():
    raise ValueError("boom")

def caught():
    try:
        fail()
    except ValueError:
        return "ok"

def numbers(count):
    for value in range(count):
        leaf(value)
        yield value

def consume():
    return sum(numbers(3))

def early_close():
    for value in numbers(5):
        if value == 1:
            break

def first_match(limit):
    for value in numbers(5):
        if value >= limit:
            return value

def search_many():
    for _ in range(4):
        first_match(1)

def two_copies():
    return list(zip(numbers(2), numbers(3)))

def outer():
    leaf(1)
    leaf(2)

def repeat_outer():
    for _ in range(3):
        outer()

def guarded():
    try:
        yield 1
        yield 2
    finally:
        leaf(0)

def close_guarded():
    generator = guarded()
    next(generator)
    generator.close()

def throw_into():
    generator = guarded()
    next(generator)
    try:
        generator.throw(KeyError("k"))
    except KeyError:
        pass

def define_class():
    class Local:
        def method(self):
            return leaf(1)
    return Local().method()

def with_lambda():
    return list(map(lambda value: leaf(value), [1, 2]))

def in_thread():
    worker = threading.Thread(target=leaf, args=(1,))
    worker.start()
    worker.join()
    leaf(2)

def recurse(depth):
    return depth if depth == 0 else recurse(depth - 1)

def exit_now():
    raise SystemExit(3)

async def coroutine():
    return 1

def run_async():
    asyncio.run(coroutine())
'''


def module_table(project_root: Path, package: str) -> dict[str, str]:
    """Absolute file path -> module name, for every file of ``package``."""
    layout = find_package_files(project_root, package)
    source_root = project_root / layout.source_root
    return {
        str(project_root / file): module_name_from_path((project_root / file).relative_to(source_root).as_posix())
        for file in layout.files
    }


def rows(trace: RecordedTrace) -> list[tuple[int | None, str, str, int]]:
    """Compact view of the steps: (parent, short key, outcome, count)."""
    return [(step.parent, step.key.split(":")[-1], step.outcome, step.count) for step in trace.steps]


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> tuple[ModuleType, dict[str, str]]:
    """The synthetic ``tracecases`` package, imported, and its module table."""
    root = tmp_path_factory.mktemp("cases").resolve()
    (root / "tracecases").mkdir()
    (root / "tracecases" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tracecases" / "cases.py").write_text(CASES_SOURCE, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module("tracecases.cases")
    finally:
        sys.path.remove(str(root))
    return module, module_table(root, "tracecases")


def trace_case(cases: tuple[ModuleType, dict[str, str]], name: str, *args: object, **options: object) -> RecordedTrace:
    """Trace one function of the synthetic package."""
    module, table = cases
    return record_trace(getattr(module, name), args, module_of_file=table, known_keys=set(), **options)


# --- the fixture package ------------------------------------------------------


def test_fixture_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """Golden steps of a scenario on the fixture, including a call the static graph misses."""
    monkeypatch.syspath_prepend(str(FIXTURE_ROOT))
    from sample.builders import build_chain
    from sample.noise.gaussian import GaussianNoise

    def scenario() -> None:
        build_chain(["b", "a"])
        GaussianNoise(scale=1.0).mean_of(50)

    known = {"sample.builders:build_chain", "sample.core:Graph.add_node"}
    trace = record_trace(scenario, module_of_file=module_table(FIXTURE_ROOT, "sample"), known_keys=known)
    assert (trace.truncated, trace.error) == (False, None)
    assert rows(trace) == [
        (None, "build_chain", "return", 1),
        (0, "Graph.__init__", "return", 1),
        (0, "Graph.add_node", "return", 2),  # the sort lambda is folded away
        (0, "build_chain.<locals>.link", "return", 1),
        (3, "Graph.add_edge", "return", 1),
        (4, "Graph._check_exists", "return", 2),
        (None, "GaussianNoise.__init__", "return", 1),
        (6, "Noise.__init__", "return", 1),
        (None, "GaussianNoise.mean_of", "return", 1),
        (8, "Noise.sample_many", "return", 1),
        (9, "GaussianNoise.sample", "return", 50),  # polymorphic: runtime-only edge
    ]
    assert [step.depth for step in trace.steps[:6]] == [0, 1, 1, 1, 2, 3]
    assert {step.key for step in trace.steps if step.matched} == known


# --- synthetic cases --------------------------------------------------------------


def test_caught_and_uncaught_exceptions(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """``fail`` is left by its exception; ``caught`` catches it and returns."""
    trace = trace_case(cases, "caught")
    assert [(step.key.split(":")[-1], step.outcome, step.exception) for step in trace.steps] == [
        ("caught", "return", None),
        ("fail", "raise", "ValueError"),
    ]


def test_exception_ending_the_target_is_reported(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """An escaping exception (even SystemExit) is named in ``error``; steps are kept."""
    assert trace_case(cases, "fail").error == "ValueError: boom"
    trace = trace_case(cases, "exit_now")
    assert (trace.error, rows(trace)) == ("SystemExit: 3", [(None, "exit_now", "raise", 1)])


def test_generator_resumes_continue_one_step(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """A generator resumed three times is one step, its calls are its children."""
    assert rows(trace_case(cases, "consume")) == [
        (None, "consume", "return", 1),
        (0, "numbers", "return", 1),
        (1, "leaf", "return", 3),
    ]


def test_generator_abandoned_by_break_is_unfinished(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """``break`` closes the generator silently (no event in 3.12): left ``unfinished``."""
    assert rows(trace_case(cases, "early_close")) == [
        (None, "early_close", "return", 1),
        (0, "numbers", "unfinished", 1),
        (1, "leaf", "return", 2),
    ]


def test_abandoned_generator_does_not_block_merging(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Four identical searches, each leaving its generator by ``return``, merge into one step."""
    assert rows(trace_case(cases, "search_many")) == [
        (None, "search_many", "return", 1),
        (0, "first_match", "return", 4),
        (1, "numbers", "unfinished", 1),
        (2, "leaf", "return", 2),
    ]


def test_two_live_copies_of_a_generator_are_kept_apart(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """In ``zip(numbers(2), numbers(3))`` the first copy ends, the second is abandoned."""
    assert rows(trace_case(cases, "two_copies")) == [
        (None, "two_copies", "return", 1),
        (0, "numbers", "return", 1),
        (1, "leaf", "return", 2),
        (0, "numbers", "unfinished", 1),
        (3, "leaf", "return", 2),
    ]


def test_identical_subtrees_merge(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Three calls of ``outer``, each calling ``leaf`` twice, give ``outer x3 -> leaf x2``."""
    assert rows(trace_case(cases, "repeat_outer")) == [
        (None, "repeat_outer", "return", 1),
        (0, "outer", "return", 3),
        (1, "leaf", "return", 2),
    ]


def test_generator_close_and_throw(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """``close()`` is a normal end; ``throw()`` of an uncaught error is a raise; ``finally`` runs inside."""
    expected_child = (1, "leaf", "return", 1)
    assert rows(trace_case(cases, "close_guarded"))[1:] == [(0, "guarded", "return", 1), expected_child]
    assert rows(trace_case(cases, "throw_into"))[1:] == [(0, "guarded", "raise", 1), expected_child]


def test_class_body_is_not_a_call(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Running a ``class`` statement inside a function is not recorded; its methods are."""
    assert rows(trace_case(cases, "define_class")) == [
        (None, "define_class", "return", 1),
        (0, "define_class.<locals>.Local.method", "return", 1),
        (1, "leaf", "return", 1),
    ]


def test_lambda_is_folded_into_its_function(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Calls made inside a lambda belong to the function containing it."""
    assert rows(trace_case(cases, "with_lambda")) == [(None, "with_lambda", "return", 1), (0, "leaf", "return", 2)]


def test_other_threads_are_ignored(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Only the calling thread's ``leaf(2)`` is recorded, not the worker's ``leaf(1)``."""
    assert rows(trace_case(cases, "in_thread")) == [(None, "in_thread", "return", 1), (0, "leaf", "return", 1)]


def test_step_cap_truncates(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Recording stops at the cap; the calls still open are ``unfinished``."""
    trace = trace_case(cases, "recurse", 10, max_steps=4)
    assert trace.truncated
    assert [(step.depth, step.outcome) for step in trace.steps] == [(depth, "unfinished") for depth in range(4)]


def test_async_code_is_refused(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """Async code raises a clear error instead of giving a wrong tree."""
    with pytest.raises(TraceError, match="async code is not supported"):
        trace_case(cases, "run_async")


def test_traces_are_repeatable(cases: tuple[ModuleType, dict[str, str]]) -> None:
    """A second trace gives the same steps: nothing is left switched off."""
    assert trace_case(cases, "consume") == trace_case(cases, "consume")


# --- trace files ----------------------------------------------------------------


def sample_trace_file() -> TraceFile:
    """A small hand-made trace: ``f`` calling ``g`` three times, ``g`` failing."""
    steps = (
        TraceStep(0, None, 0, "pkg.mod:f", "return", None, 1, True),
        TraceStep(1, 0, 1, "pkg.mod:g", "raise", "ValueError", 3, False),
    )
    return trace_file_for(RecordedTrace(steps, False, None), "demo", "pkg.mod:f", "abc123", False)


def test_trace_file_round_trip(tmp_path: Path) -> None:
    """Write then read gives the same trace; the header describes the recording."""
    trace = sample_trace_file()
    path = tmp_path / "traces" / "demo.json"
    write_trace(trace, path)
    assert read_trace(path) == trace
    header = trace.header
    assert (header.scenario, header.step_count, header.truncated, header.error) == ("demo", 2, False, None)
    assert header.python_version == platform.python_version()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["steps"][1]["args"] is None and data["steps"][1]["return_value"] is None
    assert len(path.read_text(encoding="utf-8").splitlines()) == 7  # one line per step


def test_empty_trace_round_trip(tmp_path: Path) -> None:
    """A scenario that calls no package function gives a valid, empty trace."""
    trace = trace_file_for(RecordedTrace((), False, "SystemExit: 0"), "empty", "x:y", None, True)
    write_trace(trace, tmp_path / "empty.json")
    assert read_trace(tmp_path / "empty.json") == trace


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data["header"].update(schema_version=99), "schema version 99"),
        (lambda data: data["header"].pop("commit"), "invalid trace content"),
        (lambda data: data["steps"][1].update(outcome="crash"), "unknown outcome"),
        (lambda data: data["steps"][1].update(parent=5), "not an earlier step"),
        (lambda data: data["steps"][1].update(depth=4), "expected 1"),
        (lambda data: data["steps"][1].update(id=7), "has id 7"),
        (lambda data: data["steps"][1].update(parent="0"), "invalid trace content"),
        (lambda data: data["header"].update(step_count=9), "says 9 steps"),
        (lambda data: data.update(steps={}), "must be a list"),
        (lambda data: data["steps"].__setitem__(1, [1, 0]), "invalid trace content"),
        (lambda data: data.update(header="x"), "invalid trace content"),
    ],
)
def test_invalid_trace_content(change: Callable[[dict], object], message: str) -> None:
    """Each kind of damage gives one clear TraceFormatError."""
    data = trace_to_dict(sample_trace_file())
    change(data)
    with pytest.raises(TraceFormatError, match=message):
        trace_from_dict(data)


def test_unreadable_trace_file(tmp_path: Path) -> None:
    """A file that is not JSON fails with TraceFormatError, not a JSON error."""
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TraceFormatError, match="not valid UTF-8 JSON"):
        read_trace(path)
