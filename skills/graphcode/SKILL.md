---
name: graphcode
description: Map a Python package's architecture (who calls, imports or inherits what) and record which functions a real call runs, with the graphcode CLI. Use when a project's pyproject.toml has a [tool.graphcode] section, when asked how a package is structured or what depends on a function, or to follow what a call actually executes.
---

# graphcode

graphcode writes everything to `<project>/.graphcode/`:

- `graph.json`: the static graph;
- `traces/<name>.json`: recorded runs;
- `viewer.html`: an offline page for the human.

You read the JSON files. You point the human at the page.

## Setup

- It must run inside the analysed project's environment, because traces import the project's libraries. If `uv run graphcode --help` fails there, ask the human before running `uv add --dev --editable <path to GraphCode>`. That command edits their `pyproject.toml` and `uv.lock`.
- The config lives in the project's `pyproject.toml`:

  ```toml
  [tool.graphcode]
  package = "mypackage"                       # import name, required
  exclude = ["mypackage/legacy"]              # optional
  [tool.graphcode.scenarios]                  # optional
  dag-and-sample = "scripts.scenarios:dag_and_sample"
  load = { target = "mypackage.io:load", args = ["x.json"] }
  ```

## Answer a structure question

1. Run `uv run graphcode build-graph`. It is fast, so rebuild before reading, since `graph.json` may be stale.
2. Query `.graphcode/graph.json` with a short script. Example, for "who calls `DAG.add_node`":

   ```bash
   uv run python -c "import json; g = json.load(open('.graphcode/graph.json'))
   print([e['source'] for e in g['edges'] if e['kind'] == 'calls' and e['target'] == 'mypackage.dag:DAG.add_node'])"
   ```

3. Treat missing call edges as unknown, not absent (see *Limits*). When the answer depends on a call through an untyped value, confirm it with a trace.

### graph.json

- **Keys:** `module.path:Qualname`, e.g. `mypackage.dag:DAG.add_node`, or `mypackage.dag` for a module. Nested definitions are written `f.<locals>.g`.
- **Nodes:**
  - fields: `key`, `kind`, `file`, `spans`, `parent`;
  - `kind` is one of `package`, `module`, `class`, `method`, `function`;
  - `spans` is a list of `[start, end]` line pairs;
  - `parent` is the containing key.
- **Edges:**
  - fields: `source`, `target`, `kind`, `confidence`, `lines`;
  - `kind` is one of `calls`, `imports`, `inherits`;
  - `confidence` is `exact`, or `inferred` when it comes from type tracking.
  - A call to a class points to its `__init__` if the class or an in-package base defines one; otherwise it points to the class.

## Follow what a call runs

1. Pick a target:
   - a function with JSON arguments: `uv run graphcode trace mypackage.io:load --args '["x.json"]' --kwargs '{"strict": true}'`;
   - an entry point with command-line words: `uv run graphcode trace mypackage.cli:main -- --rows 10`;
   - a configured scenario: `uv run graphcode trace @dag-and-sample`, or `--all` for every scenario.
2. An instance method needs an object, so it cannot be a target. Write an argument-free **scenario** function outside the package, for example in `scripts/`, that builds the object and makes the call. Register it under `[tool.graphcode.scenarios]`.
3. Read `.graphcode/traces/<name>.json`.
   - **Order:** `steps` is a flat list in execution order; `id` and `parent` rebuild the call tree.
   - **`count`:** that many identical consecutive calls merged into one step.
   - **`outcome`:** `return`, `raise` (with `exception`), or `unfinished`. `unfinished` means the step cap stopped the recording, or a generator was abandoned.
   - **Header:** `error` names the exception that ended the scenario; `truncated` is set at 50,000 steps; `commit` and `dirty` identify the code that was traced.
4. Compare with `graph.json`. A caller → callee pair with no `calls` edge is a **runtime-only** call: the static graph missed it, typically polymorphism such as `node.noise.sample()`.

## Show the human

Run `uv run graphcode view --open`, then pass on the URL options:

- `viewer.html#mode=network` opens the Network mode;
- `#trace=<name>&step=<n>` opens a trace at step `n`, counted from 1.

The page has a Classic mode (nested boxes) and a Network mode (balls, laid out by the code's structure). The Trace menu offers a call tree and a step-through player.

## Limits

- **Static calls are partial.** Calls through untyped values, polymorphic dispatch and decorators give no edge.
- **Traces:**
  - need Python 3.12 or later in the project;
  - record only the thread that started the trace;
  - stop with an error on async code;
  - do not show dataclass-generated `__init__` methods;
  - attach lambdas, generator expressions and class bodies to the function that contains them.
- **Only the configured package is graphed.** Tests, scripts and libraries are left out.
