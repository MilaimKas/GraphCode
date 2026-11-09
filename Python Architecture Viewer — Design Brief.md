# Python Architecture Viewer — Design Brief

Nov 8, 2026 · @Fluksnumberone · revised after design review (grilling session, Nov 8, 2026)

## Purpose and scope

Build a local, free viewer that shows how a Python codebase fits together and how real runs move through it, so a human can review architecture that is mostly written by Claude.

- **Name:** `graphcode` everywhere: package, import, CLI, `[tool.graphcode]` config section, `.graphcode/` output folder.
- **Users:** me and a small team, on small to medium personal projects.
- **Runs locally:** no hosted service, no code leaves the machine, no LLM needed.
- **Own extractor, validated against Graphify:** structure comes from our own `ast`-based extractor. Graphify is not a dependency; it is used only as a reference in a test (see Stage 1).
- **Out of scope for now:** multi-language support, huge codebases, LLM-based semantic links, anything outside the main package in the graph (tests, scripts, notebooks), tracing pytest tests, async code, full variable tracking.
- **Assumption: the analysed package is well structured.** Pathological code (a name redefined in the same scope, `global` definitions inside functions, dead `while False:` blocks, folders without `__init__.py`) gets a reasonable default, not special handling.

## Architecture

Three independent stages that only exchange files, so each can be built, tested and replaced on its own.

```
 source code ──► [1 Structure] ──► graph.json ─┐
                                               ├──► [3 Interface] ──► viewer.html
 scenarios  ──► [2 Behavior]  ──► traces/*.json┘        ▲
                                                        └── source code (embedded)
```

- **Structure** produces `graph.json`: every module, class and function, and what *can* call, import or inherit what.
- **Behavior** runs named scenarios and stores what *actually* ran as trace files, recorded once and replayed in the viewer.
- **Interface** reads both files plus the source code and writes one HTML page that opens offline.

## Reference projects

- **DataGenerator** (`~/Codes/DataGenerator`, ~2.6k lines, 124 definitions, flat layout): the milestone 1 target.
- **AED_rate** (`~/Codes/AED_rate`, ~12k lines, 236 definitions, nested subpackages): the second project that proves the tool is generic.

## Join key

The only contract between stages is a node key `module.path:Qualname`, e.g. `datagenerator.dag:DAG.add_node`.

- Same convention as Python entry points and as the CLI trace target, so there is one syntax to learn.
- Independent of `src` or flat layout.
- The tracer builds it from `frame.f_globals["__name__"]` and `co_qualname`. Code run as `__main__` is mapped back through stage 1's file → module table.
- Nested functions and lambdas use Python's qualified name (`outer.<locals>.inner`).
- Frames that match no node (decorator wrappers, etc.) are kept in traces as `matched: false` and listed, never dropped silently.
- **Duplicates are merged:** a key can repeat (property getter and setter, `@overload` stubs, conditional definitions). The extractor emits one node with several line spans, not one node per definition.
- **Only `def` and `class` become nodes.** Lambdas and comprehensions do not; their calls are credited to the enclosing function. This also keeps the graph independent of Python's comprehension inlining (3.12+).
- **Tracer edge cases (milestone 4):** 3.12 generic scopes (`<generic parameters of f>`), module bodies (`<module>`) and class bodies need explicit mapping rules; frames that cannot form a valid key are recorded as unmatched, never crash the tracer.

## Stage 1: structure graph

Our own extractor (~250 lines), built on Python's `ast`.

- **Scope:** only the configured `package` (in `src/<package>/` or a flat `<package>/`). Tests, scripts, notebooks and similar folders are ignored entirely. Calls and imports to code outside the package (stdlib, numpy, …) are dropped.
- **Extracts:** definitions (with kind, start and end line, parent), containment, imports, inheritance, and calls.
- **Call resolution rules:**
  1. bare names → same-module definitions or `from x import name` imports within the package;
  2. `module.func` where `module` is an imported package module;
  3. `self.method()` → methods of the enclosing class or its in-package bases (any file);
  4. `ClassName(...)` → the class / its `__init__`;
  5. *type tracking* (tagged `inferred`): local variables built from a class (`dag = DAG(); dag.add_node()`), `self.attr` typed by assignment or annotation (`__init__(self, dag: DAG)`), and annotated return types.
- **Known limit:** polymorphic calls (`node.noise.sample()`) stay unresolved. The static graph is partial by design; traces (Stage 2) measure the gap.
- **Source reader:** the extractor takes a function `path → text`, not the disk directly. On the working tree it reads files; for another git ref it uses `git show <ref>:<path>` (see PR diff).
- **Graphify as reference:** a test runs `graphify extract --code-only` on the fixture and checks that our call edges include Graphify's. Skipped when `graphify` is not on the PATH. Context: on DataGenerator, Graphify's 69 EXTRACTED calls matched a 125-line naive resolver almost exactly; its value was in 28 INFERRED edges (the `DAG` seam), which rule 5 reproduces.

### `graph.json` schema (version 1, may evolve)

```json
{
  "header": {"schema_version": 1, "graphcode_version": "0.1.0", "commit": "a1b2c3d", "dirty": false},
  "nodes": [
    {"key": "datagenerator.dag:DAG", "kind": "class", "file": "datagenerator/dag.py",
     "spans": [[31, 210]], "parent": "datagenerator.dag"}
  ],
  "edges": [
    {"source": "datagenerator.generator:DataGenerator.sample", "target": "datagenerator.dag:DAG.intervene",
     "kind": "calls", "confidence": "inferred", "lines": [88, 102]}
  ]
}
```

- `kind` (node): `package`, `module`, `class`, `method`, `function`. Later milestones add `public`, `raises`, `catches`.
- `kind` (edge): `calls`, `imports`, `inherits`. Containment is the `parent` field, not an edge.
- **One edge per `(source, target, kind)`**, with `lines` listing every occurrence. Moving a call to another line changes `lines`, not the edge, so the PR diff ignores it. `confidence` is `exact` if any occurrence is exact, else `inferred`.
- **Containment rule:** a node's `parent` is its key minus the last level (`pkg.mod:DAG.add_node` → `pkg.mod:DAG` → `pkg.mod` → `pkg`; `<locals>` is skipped). The root is the analysed package, with `parent: null`. The rule makes cycles impossible.
- **Redefinitions:** a name defined twice in the same scope keeps one node, of the kind of the *last* definition (the one that exists at run time), with the spans of both.
- `dirty: true` when built from uncommitted changes. Readers check `schema_version` and fail clearly on mismatch.
- In code: `Node` / `Edge` dataclasses. networkx is used only for algorithms (cycles, reachability) via a conversion function, not as the internal model.

## Stage 2: tracer and scenarios

- **Mechanism:** `sys.monitoring` only (Python 3.12+), filtered to files inside the package. The tracer checks the Python version at run time; the rest of `graphcode` works on 3.10+.
- **Targets:**
  - `module:function` (or static/class method) with literal `--args` (JSON list) and `--kwargs` (JSON object);
  - `module:main -- <argv...>` for CLI-style entry points;
  - `@scenario` from config;
  - the Python API: `graphcode.trace(func, *args, **kwargs)` and `with graphcode.tracing("name"):` for notebooks and scripts.
- **Scenario functions** replace pytest targets: argument-free functions written outside the package that build objects and make the call. Instance methods are traced this way.

  ```python
  # scripts/scenarios.py
  def sample_small() -> None:
      """Build a 2-node DAG and sample from it."""
      dag = DAG()
      dag.add_node("x"); dag.add_node("y"); dag.add_edge("x", "y")
      DataGenerator(dag).sample(100)
  ```

  The scenario function itself is outside the package, so its calls appear as root steps.
- **Child process:** the target runs in `python -m graphcode.trace.runner` with the project's interpreter (`sys.executable`), never inside the CLI process. Each scenario gets a fresh interpreter (reproducible `--all` runs), `sys.exit` or crashes cannot lose the trace, and the trace is written in a `finally`.
- **Generators:** a resume is a continuation of the original call, not a new step. Async code is detected and reported as unsupported.
- **Noise control:** consecutive sibling calls with identical subtrees collapse into one step with `count = N`. Hard cap of 50,000 steps sets `truncated: true`. Both configurable.
- **Output:** `.graphcode/traces/<scenario>.json` (milestone 4), `.graphcode/traces/<scenario>/<commit>.json` (milestone 5).

### Trace schema (version 1)

```json
{
  "header": {
    "schema_version": 1, "scenario": "sample-small", "target": "scripts.scenarios:sample_small",
    "date": "2026-11-08T21:14:03", "commit": "a1b2c3d", "dirty": false,
    "python_version": "3.12.7", "graphcode_version": "0.1.0", "step_count": 9, "truncated": false
  },
  "steps": [
    {"id": 4, "parent": null, "depth": 0, "key": "datagenerator.generator:DataGenerator.sample",
     "outcome": "raise", "exception": "ValueError", "count": 1, "matched": true,
     "args": null, "return_value": null},
    {"id": 5, "parent": 4, "depth": 1, "key": "datagenerator.noise:GaussianNoise.sample",
     "outcome": "return", "exception": null, "count": 100, "matched": true,
     "args": null, "return_value": null}
  ]
}
```

- Steps are a **flat list in execution order**; `id` + `parent` form the call tree (`parent: null` = called by the entry point). `depth` is redundant but saves recomputing indentation. A flat list can be streamed, truncated and compared easily.
- `outcome: "raise"` means the function was *exited* by an exception; an exception caught inside counts as `return`.
- `args` and `return_value` are reserved (always `null`) for variable tracking.

## Stage 3: interface

A Python generator writes one self-contained static HTML page (no server) with Cytoscape.js.

- **Front end:** plain JavaScript, vendored and version-pinned Cytoscape, `cytoscape-dagre` and `cytoscape-expand-collapse`, shipped inside the package. No npm, no build step.
- **Source code:** each file embedded once; the code panel shows only the selected function's or class's span. Modules show file path and size, not code.
- **Layout:** hierarchical (dagre). On open, modules are expanded and classes collapsed; edges of collapsed children are redrawn onto the box.

| Feature | What the user sees | Data it needs | Milestone |
| --- | --- | --- | --- |
| Node styles by kind | Packages, modules, classes, methods, functions in distinct colors and shapes, with a legend | Node kind | 1 |
| Edge styles by kind | Calls, imports, inheritance distinct; inferred edges dashed; legend states call edges are static and partial | Edge kind, confidence | 1 |
| Click to highlight | Clicking a node highlights its direct neighbors and dims the rest | Edges | 1 |
| Code on click | Side panel shows the function's or class's source, file and line | Code span, embedded source | 1 |
| Collapsible boxes | Package › module › class nesting; collapse or expand each | `parent` | 1 |
| Search and filter | Find a node by name; hide private functions or edge kinds | Node keys, kinds | 1 |
| Diff mode | Added / removed (ghost) / modified / unchanged colors, change badges on collapsed boxes, function diff in code panel | Two graphs | 2 |
| Public API layer | Public entry points marked; a toggle shows only what is reachable from them | `public` flag | 3 |
| Exception view | Pick an exception type and see where it is raised and caught | `raises`, `catches` | 3 |
| Trace overlay | Pick a scenario; nodes and edges on its path light up; runtime-only edges in a third style | Trace file | 4 |
| Call-tree panel | The trace as an indented list; clicking a step selects its node and shows its code | Trace file | 4 |
| Staleness warning | Banner when trace and graph commits differ, or either is dirty | `commit`, `dirty` | 4 |
| Trace diff | Calls added and removed on a scenario between two commits | Two trace files | 5 |

## PR diff

Shows how a pull request changes the network, so a reviewer sees the architectural effect of a Claude change before reading the code diff.

```
uv run graphcode diff main             # main vs current working tree
uv run graphcode diff main..feature-x  # any two refs
```

- **How:** read each ref's files straight from git (`git ls-tree -r <ref>`, `git show <ref>:<path>`) through the extractor's source reader. No checkout, no worktree, no code run. Graphs of clean commits are cached by hash in `.graphcode/cache/`; a dirty working tree is never cached.
- **Join:** nodes and edges compared by join key.
- **Modified:** a node's *structural hash* changed: the hash of its AST dump without line numbers and without docstrings. Formatting, comments and docstring edits do not count.
- **Classes:** a class is modified only if its own body changes (bases, decorators, class attributes), not when one of its methods changes. Collapsed boxes with changed children show a badge with counts.
- **Moves and renames:** a removed and an added node with the same structural hash are shown as one move.
- **Edges:** added and removed edges; new cross-module dependencies and new import cycles called out separately. Cycles use networkx strongly connected components over module-level imports only (imports inside functions and under `if TYPE_CHECKING:` are ignored).
- **Public API changes** (from milestone 3): public functions added, removed, or with a changed signature (hash of the arguments node).
- **Output:** a separate `.graphcode/diff-<base>-<head>.html` (same viewer in diff mode, union of both graphs, removed nodes as ghosts, `difflib` diff in the code panel), and `--format md` to stdout: counts, new dependencies, cycles, API changes. Local only; CI integration is out of scope.

## Static enrichment (milestone 3)

- **Public API**, own rules (no griffe): `__all__` if present, else names without a leading underscore; names re-exported by a package `__init__` are public at package level; everything inside a private module (`_internal.py`) is private. A dynamically built `__all__` is reported as `unknown`.
- **Exceptions:** direct `raise X` and `except X` per function only, plus bare `raise` (re-raise) noted. Names resolved to package classes where possible; built-ins kept as names. No propagation along call edges: static propagation over a partial call graph would be confidently incomplete; traces record actual raises.

## Packaging and config

- **Repository:** public on my GitHub, MIT licence.
- **Installation:** dev dependency in each project from a git tag: `uv add --dev git+https://github.com/<me>/GraphCode@v0.1`. Run with `uv run graphcode ...`. The tracer must run in the project's environment, which is why it is not a global `uv tool`.
- **Python:** `requires-python >=3.10`, so target projects declaring `>=3.10` resolve without edits. Only the tracer needs 3.12.
- **Dependencies:** networkx. Nothing else at run time beyond the standard library.
- **Config** in the target project's `pyproject.toml`:

  ```toml
  [tool.graphcode]
  package = "datagenerator"          # import name, required
  exclude = ["datagenerator/legacy"] # optional

  [tool.graphcode.scenarios]         # used from milestone 4
  sample-small = "scripts.scenarios:sample_small"
  load = { target = "datagenerator.dag:load", args = ["x.json"] }
  ```

  Scenario values use the CLI target syntax. Milestone 1 reads only `package` and `exclude`. Scenario functions live outside the package (e.g. `scripts/`): they are run, never drawn.
- **Commands:** `graphcode build-graph`, `graphcode view`, `graphcode diff <ref>[..<ref>]`, `graphcode trace <target> [--args ...] [--kwargs ...] [--name ...] [-- argv...]`, `graphcode trace @<scenario>`, `graphcode trace --all`.
- **Output:** everything generated goes in `.graphcode/` in the target project, git-ignored unless the team wants to share traces.

## Package structure

A `src` layout with one subpackage per stage. File list indicative; each file stays around 300 lines.

```
GraphCode/
├── pyproject.toml
├── src/graphcode/
│   ├── __init__.py          # public API: trace(), tracing()
│   ├── __main__.py          # python -m graphcode
│   ├── cli.py               # build-graph, view, diff, trace
│   ├── config.py            # reads [tool.graphcode]
│   ├── keys.py              # join key module:qualname
│   ├── sources.py           # source readers: working tree, git ref
│   ├── graph/               # stage 1: structure
│   │   ├── model.py         # Node / Edge dataclasses and graph rules
│   │   ├── serialization.py # graph.json read/write
│   │   ├── definitions.py   # definitions, kinds, spans, containment
│   │   ├── imports.py       # import edges, module table
│   │   ├── calls.py         # call resolution rules 1–5
│   │   ├── public_api.py    # milestone 3
│   │   └── exceptions.py    # milestone 3
│   ├── diff/                # PR diff
│   │   ├── hashing.py       # structural hash
│   │   ├── compare.py       # nodes, edges, moves, cycles
│   │   └── report.py        # Markdown summary
│   ├── trace/               # stage 2: behavior
│   │   ├── targets.py       # module:qualname, argv, @scenarios
│   │   ├── runner.py        # child-process entry point
│   │   ├── tracer.py        # sys.monitoring recorder + filtering + collapse
│   │   └── format.py        # trace schema read/write
│   └── view/                # stage 3: interface
│       ├── generate.py      # graph (+ traces, diff) + source -> one HTML
│       ├── template.html
│       └── assets/          # viewer.js, viewer.css, vendored Cytoscape + plugins
└── tests/
    ├── fixtures/sample_project/   # small known codebase
    ├── test_graph.py
    ├── test_graphify_reference.py # skipped without graphify
    ├── test_diff.py
    ├── test_trace.py
    └── test_view.py
```

- **Dependency rule:** `graph`, `trace` and `view` never import each other; they share only `keys.py`, `config.py`, `sources.py` and the file formats. `diff` may use `graph`.
- **Tests:** pytest with golden files on the sample fixture (expected `graph.json` fragments, expected trace steps, expected embedded data in the HTML). The JavaScript is checked by hand in milestone 1.

## Build order

Five milestones, each usable on its own before the next starts.

1. **Graph viewer.** Config, join key, extractor (definitions, imports, inheritance, calls with type tracking), `graph.json`, viewer page.
   - Done when, on DataGenerator: every package definition appears with the right kind and span (golden test on fixture, spot check on DataGenerator); the cross-module seam into `DAG` (the 28 type-inferred edges) is visible; the page opens offline and stays responsive; highlight, code panel, collapse and search work; the same works on AED_rate with only `package` configured.
2. **Structural PR diff.** Git source reader, structural hash, comparison, cycles, cache, diff mode, Markdown summary.
   - Done when: running it on a real PR shows its added, removed and modified nodes and any new dependencies at a glance.
3. **Static enrichment.** Public API flag and exception attributes, their views, API changes in the PR diff.
   - Done when: public entry points and every raised exception type show up correctly on a known project.
4. **Traces.** Targets, child-process runner, tracer, trace files, overlay with runtime-only edges, call-tree panel, staleness warning.
   - Done when: one or two important scenarios can be recorded with one command and followed step by step in the viewer.
5. **History.** Traces per commit; diff a scenario between two commits as the set of caller → callee edges with counts (no tree alignment). Old refs are re-run in a temporary git worktree with `uv run`.
   - Done when: reviewing a Claude change shows which calls it added or removed on a key path.

## Later

- **Variable tracking:** first, each call's arguments and return value as capped `repr` strings in the reserved trace fields, limited to chosen scenarios and functions. Known costs: slow for large objects, objects mutate after the call, `repr` can have side effects. Local variables per line only if needed.
- **External libraries:** a toggle showing one collapsed node per external library.
- **Docstring-only changes:** a faint marker in diff mode, if missed.
- **Tests in the graph / pytest targets:** to be designed if needed.
- **CI:** posting the Markdown diff summary from GitLab CI.

## Open questions

- [ ] Which one or two DataGenerator scenarios matter most to trace first? (needed at milestone 4)
- [ ] After milestone 4: does the runtime-only edge count justify more type tracking in the extractor?
