"""The PR diff as a short Markdown summary, to read in a terminal or paste into a pull request.

Architectural news first (new dependencies between modules, new import
cycles), then the definitions and edges that changed. Example::

    ## graphcode diff: main (a1b2c3d) → working tree (e4f5a6b, uncommitted changes)

    3 added · 0 removed · 2 modified · 1 moved · 5 edges added · 1 removed

    ### New dependencies between modules
    - `datagenerator.patterns` → `datagenerator.noise`
    ...
"""

from __future__ import annotations

from collections.abc import Sequence

from graphcode.diff.compare import GraphDiff
from graphcode.graph.model import Edge, GraphHeader

MAX_LIST_ITEMS = 40  # longer lists end with "… and N more"


def markdown_report(diff: GraphDiff, base_name: str, head_name: str | None) -> str:
    """Return the diff as Markdown.

    Parameters
    ----------
    diff : GraphDiff
        The comparison of the two graphs.
    base_name, head_name : str, str or None
        The refs as the user typed them; None for the working tree.
    """
    title = f"## graphcode diff: {_version(base_name, diff.base)} → {_version(head_name, diff.head)}"
    if diff.is_empty:
        return f"{title}\n\nNo structural change (formatting, comments and docstrings are not counted).\n"
    counts = (
        f"{len(diff.added)} added · {len(diff.removed)} removed · {len(diff.modified)} modified · "
        f"{len(diff.moved)} moved · {len(diff.added_edges)} edges added · {len(diff.removed_edges)} removed"
    )
    sections = [
        title,
        counts,
        _section("New dependencies between modules", [f"`{source}` → `{target}`" for source, target in diff.new_dependencies]),
        _section("New import cycles", [", ".join(f"`{module}`" for module in cycle) + " import each other" for cycle in diff.new_cycles]),
        _section("Added", [f"`{node.key}` ({node.kind.value})" for node in diff.added]),
        _section("Removed", [f"`{node.key}` ({node.kind.value})" for node in diff.removed]),
        _section("Modified", [f"`{change.after.key}` ({change.after.kind.value})" for change in diff.modified]),
        _section("Moved or renamed", [f"`{change.before.key}` → `{change.after.key}`" for change in diff.moved]),
        _section("Edges added", [_edge(edge) for edge in diff.added_edges]),
        _section("Edges removed", [_edge(edge) for edge in diff.removed_edges]),
    ]
    return "\n\n".join(section for section in sections if section) + "\n"


def _version(name: str | None, header: GraphHeader) -> str:
    """``main (a1b2c3d)``, or ``working tree (a1b2c3d, uncommitted changes)``."""
    details = [header.commit[:7]] if header.commit else []
    if header.dirty:
        details.append("uncommitted changes")
    label = name if name is not None else "working tree"
    return f"{label} ({', '.join(details)})" if details else label


def _edge(edge: Edge) -> str:
    """One edge as ``source → target (kind)``."""
    return f"`{edge.source}` → `{edge.target}` ({edge.kind.value})"


def _section(title: str, items: Sequence[str]) -> str:
    """A heading and a bullet list, cut after MAX_LIST_ITEMS; empty text when there are no items."""
    if not items:
        return ""
    lines = [f"### {title}", *(f"- {item}" for item in items[:MAX_LIST_ITEMS])]
    if len(items) > MAX_LIST_ITEMS:
        lines.append(f"- … and {len(items) - MAX_LIST_ITEMS} more")
    return "\n".join(lines)
