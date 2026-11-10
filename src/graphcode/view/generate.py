"""Write the viewer: one self-contained HTML page that opens offline.

The page inlines the vendored JavaScript (Cytoscape and its plugins), the
viewer's own script and style, the graph, and the text of every source file
(each embedded once; the code panel slices a definition's span out of it).

By the package's dependency rule, ``view`` does not import ``graph``: it
reads ``graph.json`` as plain JSON, the file format being the contract.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from graphcode.sources import SourceReader

# Load order matters: cytoscape before its plugin; dagre is used directly.
_SCRIPT_FILES = ("dagre.min.js", "cytoscape.min.js", "cytoscape-expand-collapse.js", "viewer.js")
_PLACEHOLDER = re.compile(r"__(TITLE|STYLE|SCRIPTS|GRAPH_DATA)__")


def render_viewer(graph: Mapping[str, Any], sources: Mapping[str, str], title: str) -> str:
    """Return the viewer page as HTML text.

    Parameters
    ----------
    graph : mapping
        Content of ``graph.json``.
    sources : mapping
        Project-relative file path -> file text, for every node's file.
    title : str
        Page title, usually the package name.
    """
    assets = files("graphcode.view") / "assets"
    scripts = "\n".join(
        f"<script>{_safe_inside_script((assets / name).read_text(encoding='utf-8'))}</script>" for name in _SCRIPT_FILES
    )
    data = json.dumps({"graph": graph, "sources": sources}, ensure_ascii=False)
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


def write_viewer(graph_path: Path, read_source: SourceReader, output_path: Path) -> Path:
    """Read ``graph.json``, embed the sources it refers to, and write the page.

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
    output_path.write_text(render_viewer(graph, sources, title), encoding="utf-8")
    return output_path


def _safe_inside_script(text: str) -> str:
    """Escape ``</`` so that embedded text cannot close the ``<script>`` element.

    ``<\\/`` means the same as ``</`` inside JSON and JavaScript strings.
    """
    return text.replace("</", "<\\/")
