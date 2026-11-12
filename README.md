# GraphCode

A local viewer of a Python package's architecture, and of how real runs move through it.

- **Static graph:** modules, classes and functions, with their calls, imports and inheritance, extracted with `ast`.
- **Traces:** record which functions a call actually runs (Python 3.12+), and overlay that path on the graph.
- **Viewer:** a single self-contained HTML page that opens offline. No server is needed.

## Install

GraphCode runs inside the analysed project's environment, so traces can import its libraries:

```bash
cd ~/Codes/MyProject
uv add --dev --editable ~/Codes/GraphCode
```

## Configure

In the project's `pyproject.toml`:

```toml
[tool.graphcode]
package = "mypackage"              # import name, required
exclude = ["mypackage/legacy"]     # optional

[tool.graphcode.scenarios]         # optional, for traces
dag-and-sample = "scripts.scenarios:dag_and_sample"
load = { target = "mypackage.io:load", args = ["x.json"] }
```

A scenario is an argument-free function written outside the package. It builds what it needs and makes the call to follow.

## Use

```bash
uv run graphcode build-graph                 # write .graphcode/graph.json
uv run graphcode view --open                 # rebuild the graph, write and open .graphcode/viewer.html

uv run graphcode trace @dag-and-sample       # a scenario from pyproject.toml
uv run graphcode trace mypackage.io:load --args '["x.json"]' --kwargs '{"strict": true}'
uv run graphcode trace mypackage.cli:main -- --rows 10   # words after -- become sys.argv
uv run graphcode trace --all                 # every scenario
```

- Everything is written to `.graphcode/`, which ignores itself in git.
- Under WSL, `--open` uses the Windows browser.

## Viewer

Two modes, switched with **Classic | Network** in the toolbar:

- **Classic:** packages, modules and classes are nested boxes. Double-click a box to open or close it.
- **Network** (`viewer.html#mode=network`): a dark network of balls.
  - The colour shows the top-level module or subpackage. The size shows the number of connections.
  - The layout follows the code's structure (imports, inheritance, containment). Calls are drawn on top, so calls between distant parts of the code stand out.
  - Classes start folded (dashed ring). Double-click to show or hide their methods.

In both modes:

- Click a node to see its code, and what it uses and is used by. Drag the panel's left edge to resize it.
- Search, filter edges by kind, and hide private functions.
- **Trace** menu, or `viewer.html#trace=<name>`:
  - the call tree is shown in the side panel;
  - the nodes and calls that ran are highlighted;
  - calls missing from the static graph are added in a separate style.
- A banner warns when the trace and the graph come from different commits or from uncommitted code.

## Good to know

- **Static calls are partial.** Calls through untyped values are missing, for example `node.noise.sample()`. Traces show them.
- **Repeated identical calls merge** into one step with a count, such as `sample ×200`.
- **What is not traced:**
  - lambdas, generator expressions and class bodies: they are attached to the function that contains them;
  - dataclass-generated `__init__` methods;
  - threads other than the main one;
  - async code, which stops the trace with an error.
- **Exceptions, `sys.exit` and Ctrl-C keep the partial trace.** Its header names the error. A hard crash writes nothing.

## Development

```bash
uv run pytest          # tests; tracer tests need Python 3.12+
uvx ruff check src tests
```

The design and the roadmap are in `Python Architecture Viewer — Design Brief.md`.
