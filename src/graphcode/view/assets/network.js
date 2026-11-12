/* graphcode viewer, "Network" mode: the same graph as a force-directed network on a dark background.
 *
 * Every definition is a shaded ball: size grows with its number of connections,
 * colour is its top-level module or subpackage. Classes start folded (their
 * methods inside them); double-click a class to unfold or fold it. What to draw
 * comes from network-elements.js; this file draws it and handles interaction.
 * Positions come from Cytoscape's built-in "cose" force simulation (nodes repel,
 * edges are springs, gravity pulls to the centre). Hover or click lights a ball
 * and its neighbours; a chosen trace makes what ran glow and dims the rest.
 *
 * Own Cytoscape instance in its own container, built on first use (the force
 * layout needs a visible container). Uses what viewer.js shares on window.graphcode.
 */
(function () {
  "use strict";

  const shared = window.graphcode;
  const { data, element, showDetails } = shared;
  const { groupNames, groupColor, buildElements, visibleKey, foldedAncestors, isFolded } = shared.networkElements;
  const RUNNING_COLOR = "#ffb300";

  let networkCy = null;  // created on first switch to Network mode
  let hoveredKey = null;
  let chosenKey = null;
  const unfolded = new Set();  // classes and functions whose members are shown (double-click toggles)

  /** Look of Network mode: shaded balls with a soft halo, thin translucent edges, then trace and highlight states. */
  const networkStyle = [
    {
      selector: "node",
      style: {
        width: "data(size)", height: "data(size)", "background-image": "data(image)", "background-fit": "cover",
        "background-opacity": 0, "border-width": 0, "underlay-color": "data(color)", "underlay-padding": 3,
        "underlay-opacity": 0.12, "underlay-shape": "ellipse",
        // Small, dim labels; a label appears only once it would be at least 9 px on screen (zoom in to read more).
        label: "data(label)", color: "#9aa5b8", "font-size": 9, "text-valign": "bottom", "text-margin-y": 2,
        "text-outline-color": "#07090f", "text-outline-width": 2, "text-opacity": 0.8, "min-zoomed-font-size": 9,
      },
    },
    { selector: "node[?big]", style: { "min-zoomed-font-size": 0, "font-size": 10, color: "#c7cfdc" } },
    // a folded class: a faint dashed ring says "double-click to see my methods"
    { selector: "node[?folded]", style: { "border-width": 1.2, "border-style": "dashed", "border-color": "#c7cfdc", "border-opacity": 0.45 } },
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
    { selector: "node.lit", style: { opacity: 1, "underlay-opacity": 0.55, "underlay-padding": 8, "min-zoomed-font-size": 0, color: "#e6ebf5", "text-opacity": 1 } },
    { selector: "edge.lit", style: { opacity: 0.95, width: 2 } },
    { selector: "node.chosen", style: { "border-width": 2, "border-color": "#ffffff", "min-zoomed-font-size": 0 } },
    { selector: ".hidden", style: { display: "none" } },
  ];

  /**
   * Force-simulation settings. Springs inside a group are short and stiff, springs between groups long
   * and loose: each module gathers into a tight cluster, and clusters keep their distance.
   */
  function runForceLayout(randomize) {
    const springLength = (edge) => {
      if (edge.data("crossesGroups")) return 220;
      return edge.data("kind") === "contains" ? 30 : 70;
    };
    const springStiffness = (edge) => {
      if (edge.data("crossesGroups")) return 15;
      return edge.data("kind") === "contains" ? 150 : 100;
    };
    // Imports are drawn but left out of the simulation: modules import each other, and those
    // springs pull every module to the centre, while calls already tie the code together.
    networkCy.elements().not("edge[kind = 'imports']").layout({
      name: "cose", animate: false, randomize, fit: randomize, padding: 30, numIter: randomize ? 3000 : 1000,
      nodeRepulsion: () => 45000, nodeOverlap: 30, gravity: 0.4, componentSpacing: 120,
      idealEdgeLength: springLength, edgeElasticity: springStiffness,
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
    const hiddenIn = foldedAncestors(key, unfolded);
    if (hiddenIn.length) {  // a method inside a folded class: unfold it first
      hiddenIn.forEach((ancestor) => unfolded.add(ancestor));
      rebuild();
    }
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
    // A step is drawn on its visible ball: a method of a folded class lights the class.
    const drawnAs = (key) => (shared.nodeByKey.has(key) ? visibleKey(key, unfolded) : null);
    for (const step of steps) {
      const callee = drawnAs(step.key);
      if (!callee) continue;
      ranIds.add(callee);
      const caller = step.parent === null ? null : drawnAs(steps[step.parent].key);
      if (!caller || caller === callee) continue;
      const staticId = `calls|${caller}|${callee}`;
      const runtimeId = `runtime|${caller}|${callee}`;
      if (networkCy.getElementById(staticId).nonempty()) {
        ranIds.add(staticId);
      } else if (!ranIds.has(runtimeId)) {  // ran but absent from the static graph
        networkCy.add({ group: "edges", data: { id: runtimeId, source: caller, target: callee, kind: "calls", confidence: "runtime" } });
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

  /**
   * Redraw after folding or unfolding. Balls that stay keep their place, new ones start
   * next to the ball they came out of, and the simulation continues from there (no reshuffle).
   */
  function rebuild() {
    const oldPositions = new Map(networkCy.nodes().map((node) => [node.id(), { ...node.position() }]));
    networkCy.elements().remove();
    networkCy.add(buildElements(unfolded));
    networkCy.nodes().forEach((node) => {
      const kept = oldPositions.get(node.id());
      if (kept) { node.position(kept); return; }
      let origin = null;  // the ball it was folded in
      for (let current = shared.nodeByKey.get(node.id()).parent; current && !origin; current = shared.nodeByKey.get(current)?.parent) {
        origin = oldPositions.get(current) || null;
      }
      const start = origin || { x: 0, y: 0 };
      node.position({ x: start.x + (Math.random() - 0.5) * 60, y: start.y + (Math.random() - 0.5) * 60 });
    });
    hoveredKey = null;
    if (chosenKey) chosenKey = visibleKey(chosenKey, unfolded);
    applyFilters();
    runForceLayout(false);
    applyTrace();
    refreshHighlight();
  }

  /** Double-click: unfold a folded class, fold an unfolded one, or fold the class a member belongs to. */
  function toggleFold(key) {
    if (isFolded(key, unfolded)) {
      unfolded.add(key);
    } else if (unfolded.has(key)) {
      unfolded.delete(key);
    } else {
      const parent = shared.nodeByKey.get(key)?.parent;
      if (!parent || !unfolded.has(parent)) return;  // a module, or a plain function: nothing to fold
      unfolded.delete(parent);
    }
    rebuild();
  }

  /** Create the Network view (once, when first shown: the force layout needs a visible container). */
  function buildNetwork() {
    networkCy = cytoscape({
      container: document.getElementById("network"), elements: buildElements(unfolded), style: networkStyle, wheelSensitivity: 0.3,
    });
    networkCy.on("mouseover", "node", (event) => { hoveredKey = event.target.id(); refreshHighlight(); });
    networkCy.on("mouseout", "node", () => { hoveredKey = null; refreshHighlight(); });
    networkCy.on("tap", "node", (event) => choose(event.target.id()));
    networkCy.on("tap", (event) => { if (event.target === networkCy) choose(null); });
    networkCy.on("dbltap", "node", (event) => toggleFold(event.target.id()));
    applyFilters();
    runForceLayout(true);
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
