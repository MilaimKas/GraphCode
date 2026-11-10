"""Write the viewer: one self-contained HTML page that opens offline.

The page inlines the vendored JavaScript (Cytoscape and its plugins), the
viewer's own scripts and style, the graph, every trace of
``.graphcode/traces/``, and the text of every source file (each embedded
once; the code panel slices a definition's span out of it).

By the package's dependency rule, ``view`` imports neither ``graph`` nor
``trace``: it reads their JSON files as plain JSON, the file formats being
the contract.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

from graphcode.sources import SourceReader

# Load order matters: cytoscape before its plugin; dagre is used directly.
# trace.js after viewer.js: it uses what viewer.js exposes on window.graphcode.
_SCRIPT_FILES = ("dagre.min.js", "cytoscape.min.js", "cytoscape-expand-collapse.js", "viewer.js", "trace.js")
TRACE_SCHEMA_VERSION = 1  # the version of graphcode.trace.format this viewer can show
_PLACEHOLDER = re.compile(r"__(TITLE|STYLE|SCRIPTS|GRAPH_DATA)__")


def render_viewer(
    graph: Mapping[str, Any],
    sources: Mapping[str, str],
    title: str,
    traces: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Return the viewer page as HTML text.

    Parameters
    ----------
    graph : mapping
        Content of ``graph.json``.
    sources : mapping
        Project-relative file path -> file text, for every node's file.
    title : str
        Page title, usually the package name.
    traces : sequence of mapping
        Content of each trace file, in the order of the page's trace menu.
    """
    assets = files("graphcode.view") / "assets"
    scripts = "\n".join(
        f"<script>{_safe_inside_script((assets / name).read_text(encoding='utf-8'))}</script>" for name in _SCRIPT_FILES
    )
    data = json.dumps({"graph": graph, "sources": sources, "traces": list(traces)}, ensure_ascii=False)
    parts = {
        "TITLE": html.escape(title),
        "STYLE": (assets / "viewer.css").read_text(encoding="utf-8"),
        "SCRIPTS": scripts,
        # Every "<" escaped (valid in JSON strings): no "</script" or "<!--"
        # in source text can change how the browser reads the script element.
        "GRAPH_DATA": data.replace("<", "\\u003c"),
    }
    template = (files("graphcode.view") / "template.html").read_text(encoding="utf-8")
    # One pass over the template: text inserted for one placeholder (e.g.
    # source code mentioning "__SCRIPTS__") is never scanned again.
    return _PLACEHOLDER.sub(lambda match: parts[match.group(1)], template)


def write_viewer(
    graph_path: Path, read_source: SourceReader, output_path: Path, traces_folder: Path | None = None
) -> Path:
    """Read ``graph.json`` and the traces, embed the sources they refer to, and write the page.

    Returns
    -------
    Path
        ``output_path``, for printing.
    """
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    file_paths = sorted({node["file"] for node in graph["nodes"]})
    sources = {path: read_source(path) for path in file_paths}
    roots = [node["key"] for node in graph["nodes"] if node["parent"] is None]
    title = roots[0] if len(roots) == 1 else "graphcode"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    traces = _read_traces(traces_folder) if traces_folder is not None else []
    output_path.write_text(render_viewer(graph, sources, title, traces), encoding="utf-8")
    return output_path


def _read_traces(folder: Path) -> list[dict[str, Any]]:
    """Return every readable trace file of ``folder``, sorted by scenario name.

    A file that is not valid JSON or has another schema version is skipped
    with a warning: one bad trace must not prevent viewing the graph.
    """
    traces = []
    for path in sorted(folder.glob("*.json")):
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
            version = trace["header"]["schema_version"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"graphcode view: skipping {path.name}: unreadable ({error})", file=sys.stderr)
            continue
        if version != TRACE_SCHEMA_VERSION:
            print(f"graphcode view: skipping {path.name}: schema version {version}, record it again", file=sys.stderr)
            continue
        traces.append(trace)
    return traces


def _safe_inside_script(text: str) -> str:
    """Escape ``</`` so that embedded text cannot close the ``<script>`` element.

    ``<\\/`` means the same as ``</`` inside JSON and JavaScript strings.
    """
    return text.replace("</", "<\\/")
