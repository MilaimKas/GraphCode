/* graphcode viewer: diff mode, on a page written by `graphcode diff`.
 *
 * Each node and edge of the graph carries a status (graphcode.diff.page):
 * added (green, +), modified (orange, ~), moved-to (violet, ↔), removed or moved-from
 * (grey ghosts: the old version's nodes, dashed, − or ↔), unchanged (left as is, edges
 * faded so that changes stand out). Colours are the tokens of viewer.css. Boxes holding changes open, and their label
 * counts the changes inside. The details panel shows a definition's code diff,
 * and where a moved definition came from or went to.
 *
 * Does nothing on a normal page (no `diff` in the embedded data).
 */
(function () {
  "use strict";

  const { data, cy, expandCollapse, detailsHooks, themeHooks, token, element, collapseDefault, focus } = window.graphcode;
  const diff = data.diff;
  if (!diff) return;

  const statusOf = new Map(data.graph.nodes.map((node) => [node.key, node.status]));
  // A glyph before the label is the second cue beside colour (for readers who cannot tell the hues apart).
  const GLYPH = { added: "+", modified: "~", "moved-to": "↔", "moved-from": "↔", removed: "−" };

  /** `color` mixed into `base` by `share` (0 to 1): a pale tint. Both are "#rrggbb" tokens; Cytoscape cannot read CSS color-mix(). */
  function tint(color, base, share) {
    const channels = (hex) => [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16));
    const mixed = channels(color).map((value, index) => Math.round(value * share + channels(base)[index] * (1 - share)));
    return `#${mixed.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
  }

  /**
   * Add the diff rules to the style sheet, in the colours of the current theme.
   * Run again after a theme change, which replaces the whole style sheet.
   */
  function addDiffStyle() {
    const surface = token("surface");
    const marked = (status) => ({ "border-color": token(status), "border-width": 4, "background-color": tint(token(status), surface, 0.15) });
    cy.style()
      .selector("node[status = 'added']").style(marked("added"))
      .selector("node[status = 'modified']").style(marked("modified"))
      .selector("node[status = 'moved-to']").style(marked("moved"))
      .selector("node[status = 'removed'], node[status = 'moved-from']")
      .style({ "border-color": token("removed"), "border-style": "dashed", "background-color": surface, color: token("removed"), opacity: 0.7 })
      // Unchanged edges fade; so do edges redrawn onto a closed box (they carry no status).
      .selector("edge[status = 'unchanged'], edge[!status]").style({ opacity: 0.25 })
      .selector("edge[status = 'added']").style({ "line-color": token("added"), "target-arrow-color": token("added"), width: 3, opacity: 1 })
      .selector("edge[status = 'removed']")
      .style({ "line-color": token("removed"), "target-arrow-color": token("removed"), "line-style": "dashed", width: 2, opacity: 0.8 })
      .update();
  }
  addDiffStyle();
  themeHooks.push(addDiffStyle);

  // Open everything first: closed boxes (classes, closed by viewer.js) take their members out of the drawing.
  expandCollapse.expandAll({ layoutBy: null });

  // Statuses are copied onto the drawn elements (viewer.js only copies the fields it knows).
  cy.nodes().forEach((node) => node.data("status", statusOf.get(node.id()) || "unchanged"));
  for (const edge of data.graph.edges) {
    cy.getElementById(`${edge.kind}|${edge.source}|${edge.target}`).data("status", edge.status);
  }

  /** Whether a key changed in any way (a ghost included). */
  const changed = (key) => (statusOf.get(key) || "unchanged") !== "unchanged";

  // A changed node's label starts with its glyph; a box's label counts the changes inside it, e.g. "DAG · 3 changed".
  cy.nodes().forEach((node) => {
    const count = node.descendants().filter((member) => changed(member.id())).length;
    const glyph = GLYPH[statusOf.get(node.id())];
    const text = glyph ? `${glyph} ${node.data("label")}` : node.data("label");
    node.data("label", count > 0 ? `${text} · ${count} changed` : text);
  });

  // Then close again every box below module level without a change inside.
  // A box also stays open when an edge of its contents changed (a call can change while both ends stay the same).
  collapseDefault((box) => {
    const members = box.descendants();
    return members.some((member) => changed(member.id()))
      || members.connectedEdges().some((edge) => edge.data("status") !== "unchanged");
  });

  const banner = document.getElementById("diff-banner");
  banner.textContent = `Changes from ${diff.base_label} to ${diff.head_label}`;
  banner.hidden = false;
  document.getElementById("diff-legend").hidden = false;

  /** In the details panel: the status, where a moved definition went or came from, and the code diff. */
  detailsHooks.push((key, panel) => {
    const status = statusOf.get(key) || "unchanged";
    if (status === "unchanged") return;
    const line = element("p", `Status: ${status}`, `diff-status status-${status}`);
    if (diff.moves[key]) {
      const other = element("a", diff.moves[key]);
      other.addEventListener("click", () => focus(diff.moves[key]));
      line.append(document.createTextNode(status === "moved-to" ? " · from " : " · to "), other);
    }
    panel.insertBefore(line, panel.children[2] || null);
    const changes = diff.code[key];
    if (!changes || changes.length === 0) return;
    panel.append(element("h3", "Code changes"));
    const pre = element("pre", undefined, "code code-diff");
    for (const text of changes) {
      const kind = text.startsWith("+") ? "plus" : text.startsWith("-") ? "minus" : text.startsWith("@@") ? "hunk" : "same";
      pre.append(element("span", text, `row ${kind}`));
    }
    panel.append(pre);
  });

})();
