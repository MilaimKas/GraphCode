/* graphcode viewer, "Network" mode: the same graph as a force-directed network on a dark background.
 *
 * Every definition is a shaded ball: size grows with its number of connections,
 * colour is its subpackage. Positions come from Cytoscape's built-in "cose"
 * force simulation (nodes repel, edges are springs, gravity pulls to the centre).
 * Containment (module holds class holds method) is a short, faint spring.
 * Hover or click lights a ball and its neighbours; a chosen trace makes what
 * ran glow and dims the rest.
 *
 * Own Cytoscape instance in its own container, built on first use (the force
 * layout needs a visible container). Uses what viewer.js shares on window.graphcode.
 */
(function () {
  "use strict";

  const shared = window.graphcode;
  const { data, nodeByKey, element, showDetails, shortName, isPrivate } = shared;
  const graph = data.graph;
  const RUNNING_COLOR = "#ffb300";
  const BIG_NODE_DEGREE = 6;  // balls with at least this many connections are always labelled

  let networkCy = null;  // created on first switch to Network mode
  let hoveredKey = null;
  let chosenKey = null;

  // --- groups, colours, sizes ------------------------------------------------------

  /**
   * Group of a key: the first level below the root package, module or subpackage alike
   * ("pkg.sub.mod:f" -> "sub", "pkg.dag:DAG" -> "dag"); the root itself keeps its name.
   * Grouping by subpackages only would give a flat package (no subpackages) a single colour.
   */
  function groupOf(key) {
    const parts = key.split(":")[0].split(".");
    return parts.length >= 2 ? parts[1] : parts[0];
  }

  const groupNames = [...new Set(graph.nodes.map((node) => groupOf(node.key)))].sort();
  /** Evenly spread hues, bright enough for a dark background; starting at 200 (blue) puts the first groups in cool colours. */
  const groupColor = new Map(groupNames.map((name, index) => [name, `hsl(${Math.round((index * 360) / groupNames.length + 200) % 360}, 75%, 60%)`]));

  /** The ball as an SVG picture: a radial gradient with its bright spot up and left gives the 3D look. */
  function ballImage(color) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><defs>`
      + `<radialGradient id="g" cx="35%" cy="30%" r="75%"><stop offset="0" stop-color="#fff" stop-opacity="0.95"/>`
      + `<stop offset="0.25" stop-color="${color}"/><stop offset="1" stop-color="#000" stop-opacity="0.85"/></radialGradient></defs>`
      + `<circle cx="50" cy="50" r="49" fill="${color}"/><circle cx="50" cy="50" r="49" fill="url(#g)"/></svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  }

  const groupBall = new Map(groupNames.map((name) => [name, ballImage(groupColor.get(name))]));  // one picture per group

  /** Private: the node itself, or a class or function holding it, has a private name. */
  function isHiddenAsPrivate(key) {
    for (let current = key; current; current = nodeByKey.get(current)?.parent) {
      if (isPrivate(current)) return true;
    }
    return false;
  }

  /** Connections per key: graph edges in both directions plus containment (parent to child). */
  function connectionCounts() {
    const counts = new Map(graph.nodes.map((node) => [node.key, 0]));
    const bump = (key) => counts.set(key, counts.get(key) + 1);
    for (const edge of graph.edges) { bump(edge.source); bump(edge.target); }
    for (const node of graph.nodes) {
      if (node.parent) { bump(node.key); bump(node.parent); }
    }
    return counts;
  }

  // --- building the network ---------------------------------------------------------

  /** Cytoscape elements: one ball per node, a faint containment edge to its parent, and the graph's edges. */
  function buildElements() {
    const counts = connectionCounts();
    const elements = [];
    for (const node of graph.nodes) {
      const connections = counts.get(node.key);
      elements.push({
        group: "nodes",
        data: {
          id: node.key, label: shortName(node.key), kind: node.kind, private: isHiddenAsPrivate(node.key), connections,
          size: Math.min(70, 12 + 7 * Math.sqrt(connections)), big: connections >= BIG_NODE_DEGREE,
          image: groupBall.get(groupOf(node.key)), color: groupColor.get(groupOf(node.key)),
        },
      });
      if (node.parent) {
        elements.push({
          group: "edges",
          data: { id: `contains|${node.parent}|${node.key}`, source: node.parent, target: node.key, kind: "contains", color: groupColor.get(groupOf(node.key)) },
        });
      }
    }
    for (const edge of graph.edges) {
      elements.push({
        group: "edges",
        data: { id: `${edge.kind}|${edge.source}|${edge.target}`, source: edge.source, target: edge.target, kind: edge.kind, confidence: edge.confidence },
      });
    }
    return elements;
  }

  /** Look of Network mode: shaded balls with a soft halo, thin translucent edges, then trace and highlight states. */
  const networkStyle = [
    {
      selector: "node",
      style: {
        width: "data(size)", height: "data(size)", "background-image": "data(image)", "background-fit": "cover",
        "background-opacity": 0, "border-width": 0, "underlay-color": "data(color)", "underlay-padding": 3,
        "underlay-opacity": 0.12, "underlay-shape": "ellipse",
        label: "data(label)", color: "#e6ebf5", "font-size": 13, "text-valign": "bottom", "text-margin-y": 3,
        "text-outline-color": "#07090f", "text-outline-width": 3, "min-zoomed-font-size": 8,  // small balls: label appears when zoomed in
      },
    },
    { selector: "node[?big]", style: { "min-zoomed-font-size": 0, "font-size": 14, "font-weight": "bold" } },
    {
      selector: "edge",
      style: {
        width: 1, "curve-style": "straight", "line-color": "#8fa3c4", "target-arrow-color": "#8fa3c4",
        "target-arrow-shape": "triangle", "arrow-scale": 0.55, opacity: 0.4,
      },
    },
    { selector: "edge[kind = 'imports']", style: { "line-color": "#42a5f5", "target-arrow-color": "#42a5f5" } },
    { selector: "edge[kind = 'inherits']", style: { "line-color": "#ce93d8", "target-arrow-color": "#ce93d8", width: 1.6, opacity: 0.5 } },
    { selector: "edge[kind = 'contains']", style: { "line-color": "data(color)", "target-arrow-shape": "none", opacity: 0.2 } },
    { selector: "edge[confidence = 'inferred']", style: { "line-style": "dashed" } },
    { selector: "edge:loop", style: { "curve-style": "bezier" } },
    // trace overlay
    { selector: ".untraced", style: { opacity: 0.1 } },
    { selector: "node.ran", style: { "underlay-color": RUNNING_COLOR, "underlay-opacity": 0.5, "underlay-padding": 7 } },
    { selector: "edge.ran", style: { "line-color": RUNNING_COLOR, "target-arrow-color": RUNNING_COLOR, width: 2.2, opacity: 0.95 } },
    { selector: "edge[confidence = 'runtime']", style: { "line-style": "dotted" } },
    // hover and click
    { selector: ".dim", style: { opacity: 0.07 } },
    { selector: "node.lit", style: { opacity: 1, "underlay-opacity": 0.55, "underlay-padding": 8, "min-zoomed-font-size": 0 } },
    { selector: "edge.lit", style: { opacity: 0.95, width: 2 } },
    { selector: "node.chosen", style: { "border-width": 2, "border-color": "#ffffff", "min-zoomed-font-size": 0 } },
    { selector: ".hidden", style: { display: "none" } },
  ];

  /** Force-simulation settings: strong repulsion, springs along edges (short for containment), mild gravity. */
  function runForceLayout() {
    networkCy.layout({
      name: "cose", animate: false, randomize: true, fit: true, padding: 30, numIter: 2500,
      nodeRepulsion: () => 90000, nodeOverlap: 20, gravity: 0.6, componentSpacing: 80,
      idealEdgeLength: (edge) => (edge.data("kind") === "contains" ? 30 : 110),
      edgeElasticity: (edge) => (edge.data("kind") === "contains" ? 200 : 60),
    }).run();
  }

  // --- filters, highlight, trace -------------------------------------------------------------

  /** Apply the toolbar's edge-kind, inferred and private checkboxes. */
  function applyFilters() {
    if (!networkCy) return;
    networkCy.elements().removeClass("hidden");
    for (const box of document.querySelectorAll("input[data-edge-kind]")) {
      if (!box.checked) networkCy.edges(`[kind = '${box.dataset.edgeKind}']`).addClass("hidden");
    }
    if (!document.getElementById("show-inferred").checked) networkCy.edges("[confidence = 'inferred']").addClass("hidden");
    if (!document.getElementById("show-private").checked) networkCy.nodes("[?private]").addClass("hidden");
  }

  /** Light the hovered ball (else the chosen one) and its neighbours; dim the rest. */
  function refreshHighlight() {
    networkCy.elements().removeClass("lit dim");
    const key = hoveredKey || chosenKey;
    networkCy.nodes().removeClass("chosen");
    if (chosenKey) networkCy.getElementById(chosenKey).addClass("chosen");
    if (!key) return;
    const node = networkCy.getElementById(key);
    const lit = node.closedNeighborhood();
    networkCy.elements().not(lit).addClass("dim");
    lit.addClass("lit");
  }

  /** Make `key` the chosen ball (or none) and show its details. */
  function choose(key) {
    chosenKey = key;
    refreshHighlight();
    if (key) showDetails(key);
  }

  /** Select a ball, centre on it and show its details (search box and panel links call this). */
  function focusNode(key) {
    const node = networkCy.getElementById(key);
    if (node.empty()) return;
    node.removeClass("hidden");
    choose(key);
    networkCy.animate({ center: { eles: node }, zoom: Math.max(networkCy.zoom(), 1.2) }, { duration: 250 });
  }

  /** Show the trace picked in the toolbar menu: what ran glows, the rest dims. */
  function applyTrace() {
    if (!networkCy) return;
    networkCy.remove(networkCy.edges("[confidence = 'runtime']"));
    networkCy.elements().removeClass("ran untraced");
    const menu = document.getElementById("trace-select");
    if (menu.value === "") return;
    const steps = data.traces[Number(menu.value)].steps;
    const ranIds = new Set();
    for (const step of steps) {
      if (!nodeByKey.has(step.key)) continue;
      ranIds.add(step.key);
      const caller = step.parent === null ? null : steps[step.parent].key;
      if (!caller || !nodeByKey.has(caller)) continue;
      const staticId = `calls|${caller}|${step.key}`;
      const runtimeId = `runtime|${caller}|${step.key}`;
      if (networkCy.getElementById(staticId).nonempty()) {
        ranIds.add(staticId);
      } else if (!ranIds.has(runtimeId)) {  // ran but absent from the static graph
        networkCy.add({ group: "edges", data: { id: runtimeId, source: caller, target: step.key, kind: "calls", confidence: "runtime" } });
        ranIds.add(runtimeId);
      }
    }
    const ran = networkCy.collection([...ranIds].map((id) => networkCy.getElementById(id)));
    ran.addClass("ran");
    networkCy.elements().not(ran).addClass("untraced");
    applyFilters();  // the new runtime edges follow the "calls" checkbox too
  }

  /** After a trace change: trace.js reset the details panel, so forget the chosen ball and give Network's hint. */
  function afterTraceChange() {
    applyTrace();
    if (!networkCy || document.body.dataset.mode !== "network") return;
    chosenKey = null;
    refreshHighlight();
    document.getElementById("details").replaceChildren(element("p", "Click a ball to see its code and neighbours.", "hint"));
  }

  /** One legend line per group, with a ball of its colour. */
  function fillLegend() {
    const list = document.getElementById("network-groups");
    for (const name of groupNames) {
      const item = element("li", name);
      const ball = element("span", undefined, "ball");
      ball.style.background = `radial-gradient(circle at 35% 30%, #fff, ${groupColor.get(name)} 30%, #000 130%)`;
      item.prepend(ball);
      list.append(item);
    }
  }

  /** Create the Network view (once, when first shown: the force layout needs a visible container). */
  function buildNetwork() {
    networkCy = cytoscape({
      container: document.getElementById("network"), elements: buildElements(), style: networkStyle, wheelSensitivity: 0.3,
    });
    networkCy.on("mouseover", "node", (event) => { hoveredKey = event.target.id(); refreshHighlight(); });
    networkCy.on("mouseout", "node", () => { hoveredKey = null; refreshHighlight(); });
    networkCy.on("tap", "node", (event) => choose(event.target.id()));
    networkCy.on("tap", (event) => { if (event.target === networkCy) choose(null); });
    applyFilters();
    runForceLayout();
    applyTrace();
  }

  // --- the mode switch -------------------------------------------------------------------------

  /** Show "classic" or "network"; a choice made with the buttons is remembered in localStorage (if available). */
  function setMode(mode, remember) {
    const network = mode === "network";
    document.body.dataset.mode = mode;
    document.getElementById("graph").hidden = network;
    document.getElementById("network").hidden = !network;
    document.getElementById("network-legend").hidden = !network;
    document.getElementById("mode-classic").classList.toggle("active", !network);
    document.getElementById("mode-network").classList.toggle("active", network);
    if (network) {
      if (networkCy) networkCy.resize(); else buildNetwork();
    } else {
      shared.cy.resize();  // the classic canvas had no size while hidden
      shared.cy.fit(undefined, 20);
    }
    if (!remember) return;  // a #mode=... link must not change the visitor's default
    try { localStorage.setItem("graphcode-mode", mode); } catch (error) { /* private window: not remembered */ }
  }

  /** Mode from the URL (#mode=network), else the remembered one, else classic. */
  function initialMode() {
    if (location.hash.slice(1).split("&").includes("mode=network")) return "network";
    if (location.hash.slice(1).split("&").includes("mode=classic")) return "classic";
    try { return localStorage.getItem("graphcode-mode") === "network" ? "network" : "classic"; } catch (error) { return "classic"; }
  }

  fillLegend();
  // Search, panel links and call-tree rows call viewer.js's focus(): in Network mode, focus the ball here instead.
  shared.focusHooks.push((key) => {
    if (document.body.dataset.mode !== "network") return false;
    focusNode(key);
    return true;
  });
  document.getElementById("mode-classic").addEventListener("click", () => setMode("classic", true));
  document.getElementById("mode-network").addEventListener("click", () => setMode("network", true));
  document.getElementById("trace-select").addEventListener("change", afterTraceChange);
  for (const box of document.querySelectorAll("#toolbar input[type=checkbox]")) box.addEventListener("change", applyFilters);
  document.getElementById("fit").addEventListener("click", () => { if (networkCy && !document.getElementById("network").hidden) networkCy.fit(undefined, 30); });
  if (initialMode() === "network") setMode("network", false);
})();
