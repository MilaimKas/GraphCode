/* graphcode viewer: draws graph.json with Cytoscape.
 *
 * Packages, modules and classes are nested boxes (compound nodes, from each
 * node's `parent`). On open, classes are collapsed; the expand-collapse
 * plugin then moves the edges of hidden children onto the collapsed box.
 */
(function () {
  "use strict";

  const data = JSON.parse(document.getElementById("graph-data").textContent);
  const graph = data.graph;
  const sources = data.sources;
  const nodeByKey = new Map(graph.nodes.map((node) => [node.key, node]));
  const KIND_COLORS = {
    package: "#3949ab", module: "#1e88e5", class: "#fb8c00", method: "#43a047", function: "#00897b",
  };
  const BOX_KINDS = new Set(["package", "module"]);
  const NODES_ONLY = { includeEdges: false, includeOverlays: false };  // box size without edge curves

  // --- names ----------------------------------------------------------------

  /** Last part of a key: "pkg.mod:A.<locals>.f" -> "f"; a module keeps its dotted name. */
  function shortName(key) {
    const [module, qualname] = key.split(":");
    if (!qualname) return module;
    const parts = qualname.split(".");
    return parts[parts.length - 1];
  }

  /** Private: a leading underscore, but not a dunder like __init__. */
  function isPrivate(key) {
    const name = shortName(key).split(".").pop();
    return name.startsWith("_") && !(name.startsWith("__") && name.endsWith("__"));
  }

  // --- elements and style -----------------------------------------------------

  const elements = [];
  for (const node of graph.nodes) {
    elements.push({
      group: "nodes",
      data: {
        id: node.key, label: shortName(node.key), kind: node.kind,
        parent: node.parent || undefined, private: isPrivate(node.key),
      },
    });
  }
  for (const edge of graph.edges) {
    elements.push({
      group: "edges",
      data: {
        id: `${edge.kind}|${edge.source}|${edge.target}`, source: edge.source, target: edge.target,
        kind: edge.kind, confidence: edge.confidence,
      },
    });
  }

  const style = [
    {
      selector: "node",
      style: {
        label: "data(label)", "font-size": 11, color: "#1f2933", "text-valign": "center", "text-halign": "center",
        shape: "round-rectangle", "border-width": 1.5, "background-color": "#ffffff",
        width: (node) => Math.max(36, node.data("label").length * 6.6 + 16), height: 24,
      },
    },
    { selector: "node[kind = 'package']", style: { "border-color": KIND_COLORS.package, "background-color": "#e8eaf6" } },
    { selector: "node[kind = 'module']", style: { "border-color": KIND_COLORS.module, "background-color": "#e3f2fd" } },
    { selector: "node[kind = 'class']", style: { "border-color": KIND_COLORS.class, "background-color": "#fff3e0" } },
    { selector: "node[kind = 'method']", style: { "border-color": KIND_COLORS.method, "background-color": "#e8f5e9", shape: "ellipse" } },
    { selector: "node[kind = 'function']", style: { "border-color": KIND_COLORS.function, "background-color": "#e0f2f1", shape: "ellipse" } },
    {
      selector: ":parent",
      style: {
        "text-valign": "top", "text-halign": "center", "text-margin-y": -4, "font-weight": "bold",
        "background-opacity": 0.35, padding: 14, shape: "round-rectangle",
      },
    },
    { selector: ".cy-expand-collapse-collapsed-node", style: { "border-width": 4, "border-style": "double" } },
    {
      selector: "edge",
      style: {
        // Straight: parallel edges (e.g. several methods' calls redrawn onto
        // one collapsed class) overlap into one line instead of fanning out.
        width: 1.5, "curve-style": "straight", "target-arrow-shape": "triangle", "arrow-scale": 0.9,
        "line-color": "#52606d", "target-arrow-color": "#52606d", opacity: 0.85,
      },
    },
    { selector: "edge[kind = 'imports']", style: { width: 1, "line-color": "#1e88e5", "target-arrow-color": "#1e88e5", "target-arrow-shape": "vee" } },
    {
      selector: "edge[kind = 'inherits']",
      style: { width: 3, "line-color": "#8e24aa", "target-arrow-color": "#8e24aa", "target-arrow-fill": "hollow" },
    },
    { selector: "edge[confidence = 'inferred']", style: { "line-style": "dashed" } },
    { selector: "edge:loop", style: { "curve-style": "bezier" } },  // recursion: straight cannot draw a loop
    { selector: ".faded", style: { opacity: 0.12 } },
    { selector: "node.selected", style: { "border-width": 4, "border-color": "#d81b60" } },
    { selector: ".hidden", style: { display: "none" } },
  ];

  const cy = cytoscape({ container: document.getElementById("graph"), elements, style, wheelSensitivity: 0.3 });
  const expandCollapse = cy.expandCollapse({
    layoutBy: () => runLayout(), fisheye: false, animate: false, undoable: false, cueEnabled: true,
  });

  /** The element of `siblings` that is `node` or contains it, else null. */
  function representative(node, siblings) {
    for (let current = node; current.nonempty(); current = current.parent()) {
      if (siblings.has(current.id())) return current.id();
    }
    return null;
  }

  /**
   * Lay out `children` (one nesting level) with dagre, inside-out.
   *
   * Each expanded box is laid out first, then treated as one rectangle of
   * its final size. An edge between two boxes' contents counts as an edge
   * between the boxes. Dagre on its own does not keep nested boxes apart;
   * laying out one level at a time guarantees they never overlap.
   */
  function arrange(children) {
    children.filter((child) => child.isParent()).forEach((box) => arrange(box.children()));
    const layered = new dagre.graphlib.Graph();
    layered.setGraph({ rankdir: "LR", nodesep: 16, ranksep: 60, marginx: 0, marginy: 0 });
    layered.setDefaultEdgeLabel(() => ({}));
    const siblings = new Set(children.map((child) => child.id()));
    children.forEach((child) => {
      const box = child.boundingBox(NODES_ONLY);
      layered.setNode(child.id(), { width: box.w, height: box.h });
    });
    cy.edges().forEach((edge) => {
      const source = representative(edge.source(), siblings);
      const target = representative(edge.target(), siblings);
      if (source && target && source !== target) layered.setEdge(source, target);
    });
    dagre.layout(layered);
    children.forEach((child) => {
      const placed = layered.node(child.id());
      const box = child.boundingBox(NODES_ONLY);
      const shift = { x: placed.x - (box.x1 + box.w / 2), y: placed.y - (box.y1 + box.h / 2) };
      // A box's position follows its children: move its leaves, not the box.
      const leaves = child.isParent() ? child.descendants().filter((node) => node.isChildless()) : child;
      leaves.shift(shift);
    });
  }

  function runLayout() {
    // No cy.batch(): inside a batch, box sizes are not updated from their children.
    arrange(cy.nodes().orphans());
    cy.fit(undefined, 20);
  }

  /** Collapse every box below module level (classes, functions holding nested defs). */
  function collapseDefault() {
    const boxes = cy.nodes().filter((node) => node.isParent() && !BOX_KINDS.has(node.data("kind")));
    expandCollapse.collapse(boxes, { layoutBy: null });
    applyFilters();
    runLayout();
  }

  // --- filters ------------------------------------------------------------------

  function applyFilters() {
    cy.elements().removeClass("hidden");
    // An edge between a box and its own contents (a package __init__ importing
    // its submodules) only repeats the nesting; it stays listed in the panel.
    cy.edges()
      .filter((edge) => edge.source().ancestors().contains(edge.target()) || edge.target().ancestors().contains(edge.source()))
      .addClass("hidden");
    for (const box of document.querySelectorAll("input[data-edge-kind]")) {
      if (!box.checked) cy.edges(`[kind = '${box.dataset.edgeKind}']`).addClass("hidden");
    }
    if (!document.getElementById("show-inferred").checked) cy.edges("[confidence = 'inferred']").addClass("hidden");
    if (!document.getElementById("show-private").checked) {
      const privateNodes = cy.nodes("[?private]");
      privateNodes.union(privateNodes.descendants()).addClass("hidden");
    }
  }

  // --- highlight and side panel ---------------------------------------------------

  function clearHighlight() {
    cy.elements().removeClass("faded selected");
  }

  /** Highlight a node, its children and direct neighbours; dim the rest. */
  function select(node) {
    const group = node.union(node.descendants());
    const edges = group.connectedEdges();
    const keep = group.union(edges).union(edges.connectedNodes());
    clearHighlight();
    cy.elements().addClass("faded");
    keep.union(keep.nodes().ancestors()).removeClass("faded");
    node.addClass("selected");
    showDetails(node.id());
  }

  /** Make a node visible by expanding its collapsed ancestors, then return it. */
  function reveal(key) {
    const chain = [];
    for (let current = key; current; current = nodeByKey.get(current)?.parent) chain.unshift(current);
    let expanded = false;
    for (const ancestor of chain.slice(0, -1)) {
      const element = cy.getElementById(ancestor);
      if (element.nonempty() && expandCollapse.isExpandable(element)) {
        expandCollapse.expand(element, { layoutBy: null });
        expanded = true;
      }
    }
    if (expanded) {
      applyFilters();
      runLayout();
    }
    return cy.getElementById(key);
  }

  function focus(key) {
    const node = reveal(key);
    if (node.empty()) return;
    select(node);
    cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 1) }, { duration: 250 });
  }

  function element(tag, text, className) {
    const created = document.createElement(tag);
    if (text !== undefined) created.textContent = text;
    if (className) created.className = className;
    return created;
  }

  function showDetails(key) {
    const node = nodeByKey.get(key);
    const panel = document.getElementById("details");
    panel.replaceChildren();
    const badge = element("span", node.kind, "kind-badge");
    badge.style.background = KIND_COLORS[node.kind];
    const heading = element("h2", `${key} `);
    heading.append(badge);
    panel.append(heading);
    const lastLine = Math.max(...node.spans.map(([, end]) => end));
    panel.append(element("p", `${node.file}, ${BOX_KINDS.has(node.kind) ? `${lastLine} lines` : `line ${node.spans[0][0]}`}`, "meta"));
    if (!BOX_KINDS.has(node.kind)) panel.append(codeBlock(node));
    appendNeighbours(panel, "Uses", graph.edges.filter((edge) => edge.source === key), "target");
    appendNeighbours(panel, "Used by", graph.edges.filter((edge) => edge.target === key), "source");
  }

  /** The node's source lines, one block per span (getter and setter, @overload stubs). */
  function codeBlock(node) {
    const lines = (sources[node.file] || "").split("\n");
    const pre = element("pre", undefined, "code");
    for (const [start, end] of node.spans) {  // spans are [start, end] pairs in graph.json
      for (let number = start; number <= end; number += 1) {
        const row = element("span", undefined, "row");
        row.append(element("span", String(number), "number"), document.createTextNode(lines[number - 1] ?? ""));
        pre.append(row);
      }
    }
    return pre;
  }

  function appendNeighbours(panel, title, edges, end) {
    if (edges.length === 0) return;
    panel.append(element("h3", title));
    const list = element("ul", undefined, "neighbours");
    for (const edge of edges) {
      const item = element("li");
      const link = element("a", edge[end]);
      link.addEventListener("click", () => focus(edge[end]));
      const inferred = edge.confidence === "inferred" ? ", inferred" : "";
      item.append(link, document.createTextNode(` (${edge.kind}${inferred}, line ${edge.lines.join(", ")})`));
      list.append(item);
    }
    panel.append(list);
  }

  // --- events ---------------------------------------------------------------------

  cy.on("tap", "node", (event) => select(event.target));
  cy.on("tap", (event) => {
    if (event.target === cy) clearHighlight();
  });
  cy.on("dbltap", "node", (event) => {
    const node = event.target;
    if (expandCollapse.isExpandable(node)) expandCollapse.expand(node);
    else if (expandCollapse.isCollapsible(node) && !BOX_KINDS.has(node.data("kind"))) expandCollapse.collapse(node);
  });
  cy.on("expandcollapse.afterexpand expandcollapse.aftercollapse", "node", applyFilters);

  const search = document.getElementById("search");
  const keyList = document.getElementById("node-keys");
  for (const key of nodeByKey.keys()) keyList.append(new Option(key));
  search.addEventListener("change", () => {
    const text = search.value.trim().toLowerCase();
    if (!text) return;
    const keys = [...nodeByKey.keys()];
    const match = keys.find((key) => key.toLowerCase() === text)
      || keys.find((key) => shortName(key).toLowerCase() === text)
      || keys.find((key) => key.toLowerCase().includes(text));
    if (match) focus(match);
  });

  for (const box of document.querySelectorAll("#toolbar input[type=checkbox]")) box.addEventListener("change", applyFilters);
  document.getElementById("expand-all").addEventListener("click", () => {
    expandCollapse.expandAll({ layoutBy: null });
    applyFilters();
    runLayout();
  });
  document.getElementById("collapse-classes").addEventListener("click", collapseDefault);
  document.getElementById("fit").addEventListener("click", () => cy.fit(undefined, 20));

  collapseDefault();
})();
