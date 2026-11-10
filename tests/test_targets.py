"""Tests for trace targets: parsing, named scenarios, and preparing the call."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from graphcode.trace.targets import (
    TargetError,
    TraceTarget,
    all_scenario_targets,
    parse_target,
    prepare_call,
    scenario_target,
)

SCENARIOS = {
    "small": "scripts.scenarios:small",
    "load": {"target": "pkg.dag:DAG.load", "args": ["x.json"], "kwargs": {"strict": True}},
    "cli": {"target": "pkg.cli:main", "argv": ["--rows", 10]},
}

ENTRY_SOURCE = """
import sys
from collections.abc import Iterator

def add(a, b=0):
    return a + b

def main():
    return sys.argv[1:]

def crash():
    raise RuntimeError("boom")

class Tools:
    @staticmethod
    def double(value):
        return 2 * value

    @classmethod
    def name(cls):
        return cls.__name__

    def method(self):
        return self

LIMIT = 3
"""


@pytest.fixture
def entry_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """An importable module ``tracetargets_entry`` with a few callables."""
    (tmp_path / "tracetargets_entry.py").write_text(ENTRY_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "tracetargets_entry"
    sys.modules.pop("tracetargets_entry", None)


# --- parsing ----------------------------------------------------------------------


def test_parse_target_with_arguments() -> None:
    """JSON arguments are decoded; the name defaults to the qualname with dashes."""
    target = parse_target("pkg.dag:DAG.from_file", args_json='["x.json"]', kwargs_json='{"strict": true}')
    assert target == TraceTarget("DAG-from_file", "pkg.dag:DAG.from_file", ("x.json",), {"strict": True}, None)
    assert parse_target("pkg.cli:main", argv=["--rows", "10"], name="cli").argv == ("--rows", "10")


@pytest.mark.parametrize(
    ("text", "options", "message"),
    [
        ("pkg.dag", {}, "names a module"),
        ("pkg dag:f", {}, "invalid target"),
        ("pkg.m:f.<locals>.g", {}, "nested function"),
        ("pkg.m:f", {"args_json": '{"a": 1}'}, "JSON list"),
        ("pkg.m:f", {"kwargs_json": "[1]"}, "JSON object"),
        ("pkg.m:f", {"args_json": "[1,"}, "JSON list"),
        ("pkg.m:f", {"name": "../x"}, "plain file name"),
        ("pkg.m:f", {"name": "a/b"}, "plain file name"),
    ],
)
def test_invalid_targets(text: str, options: dict[str, str], message: str) -> None:
    """Each mistake gives a TargetError that says what to write instead."""
    with pytest.raises(TargetError, match=message):
        parse_target(text, **options)


# --- named scenarios --------------------------------------------------------------


def test_scenarios_in_both_forms() -> None:
    """A plain string or a table; ``@`` is optional; argv words become strings."""
    assert scenario_target(SCENARIOS, "@small") == TraceTarget("small", "scripts.scenarios:small")
    assert scenario_target(SCENARIOS, "load").args == ("x.json",)
    assert scenario_target(SCENARIOS, "@cli").argv == ("--rows", "10")
    assert [target.name for target in all_scenario_targets(SCENARIOS)] == ["cli", "load", "small"]


@pytest.mark.parametrize(
    ("scenarios", "message"),
    [
        ({}, "none configured"),
        ({"x": 3}, "must be"),
        ({"x": {"args": []}}, "must be"),
        ({"x": {"target": "a:f", "args": "oops"}}, "must be lists"),
        ({"x": {"target": "a:f", "arg": [1]}}, "unknown field"),
    ],
)
def test_invalid_scenarios(scenarios: dict[str, object], message: str) -> None:
    """Unknown names and malformed entries are reported clearly."""
    with pytest.raises(TargetError, match=message):
        scenario_target(scenarios, "@x")


# --- preparing the call -----------------------------------------------------------


def test_prepare_call_runs_functions_and_static_methods(entry_module: str) -> None:
    """Module functions, static and class methods are called with the arguments."""
    assert prepare_call(TraceTarget("t", f"{entry_module}:add", (2,), {"b": 3}))() == 5
    assert prepare_call(TraceTarget("t", f"{entry_module}:Tools.double", (4,)))() == 8
    assert prepare_call(TraceTarget("t", f"{entry_module}:Tools.name"))() == "Tools"


def test_prepare_call_sets_argv_only_during_the_call(entry_module: str) -> None:
    """An entry point sees its words in ``sys.argv``; the old value comes back after."""
    saved = list(sys.argv)
    assert prepare_call(TraceTarget("t", f"{entry_module}:main", argv=("--rows", "10")))() == ["--rows", "10"]
    assert sys.argv == saved
    with pytest.raises(RuntimeError):
        prepare_call(TraceTarget("t", f"{entry_module}:crash", argv=("--x",)))()
    assert sys.argv == saved


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("no_such_module_xyz:f", "cannot import"),
        ("{module}:missing", "has no 'missing'"),
        ("{module}:Tools.dubble", "Tools has no 'dubble'"),
        ("{module}:Tools.method", "instance method"),
        ("{module}:LIMIT", "not a function"),
    ],
)
def test_prepare_call_errors(entry_module: str, reference: str, message: str) -> None:
    """Missing modules and names fail before anything runs."""
    with pytest.raises(TargetError, match=message):
        prepare_call(TraceTarget("t", reference.format(module=entry_module)))
