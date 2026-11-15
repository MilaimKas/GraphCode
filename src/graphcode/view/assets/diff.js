/* graphcode viewer: diff mode, on a page written by `graphcode diff`.
 *
 * Each node and edge of the graph carries a status (graphcode.diff.page):
 * added (green), modified (orange), moved-to (purple), removed or moved-from
 * (grey ghosts: the old version's nodes, dashed), unchanged (left as is, edges
 * faded so that changes stand out). Boxes holding changes open, and their label
 * counts the changes inside. The details panel shows a definition's code diff,
 * and where a moved definition came from or went to.
 *
 * Does nothing on a normal page (no `diff` in the embedded data).
 */
(function () {
  "use strict";

  const { data, cy, expandCollapse, detailsHooks, element, collapseDefault, focus } = window.graphcode;
  const diff = data.diff;
  if (!diff) return;

  const ADDED = "#2e7d32";
  const MODIFIED = "#ef6c00";
  const MOVED = "#6a1b9a";
  const GHOST = "#9e9e9e";
  const statusOf = new Map(data.graph.nodes.map((node) => [node.key, node.status]));

  cy.style()
    .selector("node[status = 'added']").style({ "border-color": ADDED, "border-width": 4, "background-color": "#e8f5e9" })
    .selector("node[status = 'modified']").style({ "border-color": MODIFIED, "border-width": 4, "background-color": "#fff3e0" })
    .selector("node[status = 'moved-to']").style({ "border-color": MOVED, "border-width": 4, "background-color": "#f3e5f5" })
    .selector("node[status = 'removed'], node[status = 'moved-from']")
    .style({ "border-color": GHOST, "border-style": "dashed", "background-color": "#fafafa", color: GHOST, opacity: 0.7 })
    .selector("edge[status = 'unchanged']").style({ opacity: 0.25 })
    .selector("edge[status = 'added']").style({ "line-color": ADDED, "target-arrow-color": ADDED, width: 3, opacity: 1 })
    .selector("edge[status = 'removed']")
    .style({ "line-color": GHOST, "target-arrow-color": GHOST, "line-style": "dashed", width: 2, opacity: 0.8 })
    .update();

  // Open everything first: closed boxes (classes, closed by viewer.js) take their members out of the drawing.
  expandCollapse.expandAll({ layoutBy: null });

  // Statuses are copied onto the drawn elements (viewer.js only copies the fields it knows).
  cy.nodes().forEach((node) => node.data("status", statusOf.get(node.id()) || "unchanged"));
  for (const edge of data.graph.edges) {
    cy.getElementById(`${edge.kind}|${edge.source}|${edge.target}`).data("status", edge.status);
  }

  /** Whether a key changed in any way (a ghost included). */
  const changed = (key) => (statusOf.get(key) || "unchanged") !== "unchanged";

  // A box's label counts the changes inside it, e.g. "DAG · 3 changed".
  cy.nodes().forEach((node) => {
    const count = node.descendants().filter((member) => changed(member.id())).length;
    if (count > 0) node.data("label", `${node.data("label")} · ${count} changed`);
  });

  // Then close again every box below module level without a change inside.
  collapseDefault((box) => box.descendants().some((member) => changed(member.id())));

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
