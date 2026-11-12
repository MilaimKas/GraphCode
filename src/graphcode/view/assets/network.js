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
  const { groupNames, groupColor, networkStyle, buildElements, visibleKey, foldedAncestors, isFolded } = shared.networkElements;
  let networkCy = null;  // created on first switch to Network mode
  let hoveredKey = null;
  let chosenKey = null;
  let currentStep = null;  // the player's current step: { key, callerKey }, or null
  const unfolded = new Set();  // classes and functions whose members are shown (double-click toggles)

  /** A number in [0, 1) computed from a text: the same text always gives the same number. */
  function stableFraction(text) {
    let hash = 0;
    for (const character of text) hash = (hash * 31 + character.codePointAt(0)) % 1000003;
    return hash / 1000003;
  }

  /**
   * Starting positions without chance, so every reload gives the same picture: the groups on a
   * circle, each ball near its group's spot, shifted by an amount computed from its name.
   */
  function placeAtStart() {
    const radius = 120 * groupNames.length;
    networkCy.nodes().forEach((node) => {
      const angle = (2 * Math.PI * groupNames.indexOf(node.data("groupName"))) / groupNames.length;
      const spread = 2 * Math.PI * stableFraction(node.id());
      node.position({
        x: radius * Math.cos(angle) + 80 * Math.cos(spread),
        y: radius * Math.sin(angle) + 80 * Math.sin(spread),
      });
    });
  }

  /**
   * Force simulation on the code's structure as written: imports, inheritance and containment.
   * Calls are drawn on top but do not move anything, so a call between distant balls shows
   * coupling across the structure. Springs inside a group are short and stiff, between groups
   * long and loose.
   */
  function runForceLayout() {
    const springLength = (edge) => {
      if (edge.data("crossesGroups")) return 140;
      return edge.data("kind") === "contains" ? 30 : 70;
    };
    const springStiffness = (edge) => {
      if (edge.data("crossesGroups")) return 30;
      return edge.data("kind") === "contains" ? 150 : 100;
    };
    placeAtStart();
    networkCy.elements().not("edge[kind = 'calls']").layout({
      name: "cose", animate: false, randomize: false, fit: true, padding: 30, numIter: 3000,
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
   * Redraw after folding or unfolding, without moving anything already drawn: the
   * simulation is not run again. Balls that come out of a class are placed on a small
   * ring around it; balls folded back simply disappear.
   */
  function rebuild() {
    const oldPositions = new Map(networkCy.nodes().map((node) => [node.id(), { ...node.position() }]));
    networkCy.elements().remove();
    networkCy.add(buildElements(unfolded));
    const newByOrigin = new Map();  // ball they came out of -> new balls
    networkCy.nodes().forEach((node) => {
      const kept = oldPositions.get(node.id());
      if (kept) { node.position(kept); return; }
      let origin = shared.nodeByKey.get(node.id()).parent;
      while (origin && !oldPositions.has(origin)) origin = shared.nodeByKey.get(origin)?.parent;
      if (!newByOrigin.has(origin)) newByOrigin.set(origin, []);
      newByOrigin.get(origin).push(node);
    });
    for (const [origin, balls] of newByOrigin) placeOnRing(oldPositions.get(origin) || { x: 0, y: 0 }, balls);
    hoveredKey = null;
    if (chosenKey) chosenKey = visibleKey(chosenKey, unfolded);
    applyFilters();
    applyTrace();
    refreshHighlight();
    showCurrentStep(false);
  }

  /** Light the player's current step on its visible balls (a method of a folded class lights the class). */
  function showCurrentStep(panToIt) {
    if (!networkCy) return;
    networkCy.elements().removeClass("current");
    if (!currentStep || !shared.nodeByKey.has(currentStep.key)) return;
    const key = visibleKey(currentStep.key, unfolded);
    const node = networkCy.getElementById(key);
    node.addClass("current");
    const callerKey = currentStep.callerKey && shared.nodeByKey.has(currentStep.callerKey)
      ? visibleKey(currentStep.callerKey, unfolded) : null;
    if (callerKey && callerKey !== key) {
      networkCy.getElementById(`calls|${callerKey}|${key}`).union(networkCy.getElementById(`runtime|${callerKey}|${key}`)).addClass("current");
    }
    const shown = networkCy.extent();
    const { x, y } = node.position();
    const offScreen = x < shown.x1 || x > shown.x2 || y < shown.y1 || y > shown.y2;
    if (panToIt && offScreen) networkCy.animate({ center: { eles: node } }, { duration: 300 });
  }

  /** Spread `balls` evenly on a circle around `centre`, wider when there are more of them. */
  function placeOnRing(centre, balls) {
    const radius = 40 + 6 * balls.length;
    balls.forEach((ball, index) => {
      const angle = (2 * Math.PI * index) / balls.length;
      ball.position({ x: centre.x + radius * Math.cos(angle), y: centre.y + radius * Math.sin(angle) });
    });
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
  shared.stepHooks.push((key, callerKey) => {
    currentStep = key === null ? null : { key, callerKey };
    if (document.body.dataset.mode !== "network") return;
    if (key !== null && chosenKey) {  // the details panel now shows the step: drop the old chosen ball
      chosenKey = null;
      refreshHighlight();
    }
    showCurrentStep(true);
  });
  document.getElementById("mode-classic").addEventListener("click", () => setMode("classic", true));
  document.getElementById("mode-network").addEventListener("click", () => setMode("network", true));
  document.getElementById("trace-select").addEventListener("change", afterTraceChange);
  for (const box of document.querySelectorAll("#toolbar input[type=checkbox]")) box.addEventListener("change", applyFilters);
  document.getElementById("fit").addEventListener("click", () => { if (networkCy && !document.getElementById("network").hidden) networkCy.fit(undefined, 30); });
  if (initialMode() === "network") setMode("network", false);
})();
