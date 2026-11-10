"""Tests for the runner: real traces in a new Python process, on a copy of the fixture."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from graphcode.cli import main as cli_main
from graphcode.trace.format import read_trace
from graphcode.trace.runner import trace_in_child
from graphcode.trace.targets import TraceTarget

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"

SCENARIOS_SOURCE = """
from sample.core import Graph
from sample.noise.gaussian import GaussianNoise


def noise_mean():
    print("scenario output")
    GaussianNoise(scale=1.0).mean_of(20)


def broken():
    Graph().add_edge("a", "b")


def exit_with_error():
    Graph()
    raise SystemExit(3)


def main():
    import sys
    for name in sys.argv[1:]:
        Graph().add_node(name)
"""

SCENARIOS_CONFIG = """
[tool.graphcode.scenarios]
noise = "scripts.scenarios:noise_mean"
cli = { target = "scripts.scenarios:main", argv = ["a", "b"] }
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of the fixture with ``scripts/scenarios.py`` and its graph built."""
    root = Path(shutil.copytree(FIXTURE_ROOT, tmp_path / "sample_project"))
    (root / "scripts").mkdir()
    (root / "scripts" / "scenarios.py").write_text(SCENARIOS_SOURCE, encoding="utf-8")
    with (root / "pyproject.toml").open("a", encoding="utf-8") as pyproject:
        pyproject.write(SCENARIOS_CONFIG)
    assert cli_main(["build-graph", "--project", str(root)]) == 0
    return root


def test_trace_in_child_writes_the_trace(project: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """The scenario runs in a new process; every step is a node of the graph (matched)."""
    assert trace_in_child(project, TraceTarget("noise-mean", "scripts.scenarios:noise_mean")) == 0
    output = capfd.readouterr().out
    assert "scenario output" in output
    assert "5 steps ->" in output
    trace = read_trace(project / ".graphcode" / "traces" / "noise-mean.json")
    assert trace.header.target == "scripts.scenarios:noise_mean"
    assert [(step.key.split(":")[1], step.count, step.matched) for step in trace.steps] == [
        ("GaussianNoise.__init__", 1, True),
        ("Noise.__init__", 1, True),
        ("GaussianNoise.mean_of", 1, True),
        ("Noise.sample_many", 1, True),
        ("GaussianNoise.sample", 20, True),
    ]


def test_a_crashing_scenario_still_writes_its_trace(project: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """The exception is named in the summary and the header; the steps up to it are kept."""
    assert trace_in_child(project, TraceTarget("broken", "scripts.scenarios:broken")) == 0
    assert "the scenario raised GraphError: unknown node a" in capfd.readouterr().out
    trace = read_trace(project / ".graphcode" / "traces" / "broken.json")
    assert trace.header.error == "GraphError: unknown node a"
    assert [(step.key.split(":")[1], step.outcome) for step in trace.steps] == [
        ("Graph.__init__", "return"),
        ("Graph.add_edge", "raise"),
        ("Graph._check_exists", "raise"),
    ]


def test_a_failing_exit_code_is_reported(project: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """``sys.exit(3)`` in the scenario is an error in the trace, not a lost trace."""
    assert trace_in_child(project, TraceTarget("exit", "scripts.scenarios:exit_with_error")) == 0
    assert "the scenario raised SystemExit: 3" in capfd.readouterr().out
    assert read_trace(project / ".graphcode" / "traces" / "exit.json").header.error == "SystemExit: 3"


def test_src_layout_without_a_graph(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """A ``src/`` package is found and recorded; with no graph.json the summary says steps are unmatched."""
    files = {
        "pyproject.toml": '[tool.graphcode]\npackage = "srcpkg"\n',
        "src/srcpkg/__init__.py": "",
        "src/srcpkg/work.py": "def helper():\n    return 1\n\ndef run():\n    return helper() + helper()\n",
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    assert trace_in_child(tmp_path, TraceTarget("run", "srcpkg.work:run")) == 0
    assert "steps not matched to the graph" in capfd.readouterr().out
    trace = read_trace(tmp_path / ".graphcode" / "traces" / "run.json")
    assert [(step.key, step.count, step.matched) for step in trace.steps] == [
        ("srcpkg.work:run", 1, False),
        ("srcpkg.work:helper", 2, False),
    ]


def test_a_wrong_target_writes_nothing(project: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """A target that cannot be found stops before running, with exit code 1 and a message."""
    assert trace_in_child(project, TraceTarget("missing", "scripts.scenarios:missing")) == 1
    assert "has no 'missing'" in capfd.readouterr().err
    assert not (project / ".graphcode" / "traces" / "missing.json").exists()


# --- the trace command ------------------------------------------------------------


def test_cli_trace_with_entry_point_words(project: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """Words after ``--`` reach the traced function's ``sys.argv`` (one new graph per word).

    The calls alternate (``__init__``, ``add_node``, ``__init__``...), so they do not merge:
    only consecutive identical calls do.
    """
    arguments = ["trace", "--project", str(project), "--no-build", "scripts.scenarios:main", "--name", "two", "--"]
    assert cli_main([*arguments, "x", "y"]) == 0
    trace = read_trace(project / ".graphcode" / "traces" / "two.json")
    assert [step.key.split(":")[1] for step in trace.steps] == ["Graph.__init__", "Graph.add_node"] * 2


def test_cli_trace_all_scenarios(project: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """``--all`` writes one trace per scenario of pyproject.toml."""
    assert cli_main(["trace", "--project", str(project), "--all"]) == 0
    assert sorted(path.name for path in (project / ".graphcode" / "traces").iterdir()) == ["cli.json", "noise.json"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["@unknown"], "no scenario 'unknown'"),
        (["pkg.mod"], "names a module"),
    ],
)
def test_cli_trace_errors(
    project: Path, capfd: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    """Wrong targets exit with 1 and a message, before any process is started."""
    assert cli_main(["trace", "--project", str(project), "--no-build", *arguments]) == 1
    assert message in capfd.readouterr().err
