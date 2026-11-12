/* graphcode viewer, Network mode: what to draw (groups, colours, ball pictures, folded classes, style).
 *
 * A class, or a function holding nested definitions, is "folded" unless the user
 * unfolded it: its members are not drawn, and their edges are drawn to and from
 * the class instead (as Classic mode does with collapsed boxes). network.js asks
 * for the elements with buildElements(unfolded), `unfolded` being a Set of keys.
 *
 * Example, with DAG folded: the edge "DataGenerator.sample -> DAG.add_node" is
 * drawn as "DataGenerator.sample -> DAG"; two such edges become one.
 */
(function () {
  "use strict";

  const { data, nodeByKey, shortName, isPrivate } = window.graphcode;
  const graph = data.graph;
  const BIG_NODE_DEGREE = 10;  // balls with at least this many connections are always labelled
  const ALWAYS_OPEN_KINDS = new Set(["package", "module"]);  // their members are always drawn

  const memberCount = new Map();
  for (const node of graph.nodes) {
    if (node.parent) memberCount.set(node.parent, (memberCount.get(node.parent) || 0) + 1);
  }

  // --- groups and colours ------------------------------------------------------------

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

  // --- folding ------------------------------------------------------------------------

  /** Whether `key` is a class or function with members that are not shown. */
  function isFolded(key, unfolded) {
    return memberCount.has(key) && !ALWAYS_OPEN_KINDS.has(nodeByKey.get(key).kind) && !unfolded.has(key);
  }

  /** The key drawn for `key`: itself, or the outermost folded class or function holding it. */
  function visibleKey(key, unfolded) {
    let shown = key;
    for (let current = nodeByKey.get(key)?.parent; current; current = nodeByKey.get(current)?.parent) {
      if (isFolded(current, unfolded)) shown = current;
    }
    return shown;
  }

  /** The folded classes and functions holding `key`, outermost first (to unfold them all). */
  function foldedAncestors(key, unfolded) {
    const ancestors = [];
    for (let current = nodeByKey.get(key)?.parent; current; current = nodeByKey.get(current)?.parent) {
      if (isFolded(current, unfolded)) ancestors.unshift(current);
    }
    return ancestors;
  }

  // --- elements -------------------------------------------------------------------------

  /** Cytoscape elements: the visible balls, their edges (members' edges moved onto folded classes), containment. */
  function buildElements(unfolded) {
    const shown = graph.nodes.filter((node) => visibleKey(node.key, unfolded) === node.key);
    const edges = new Map();  // id -> edge data; several members' edges merge into one
    for (const edge of graph.edges) {
      const source = visibleKey(edge.source, unfolded);
      const target = visibleKey(edge.target, unfolded);
      if (source === target) continue;  // e.g. a method calling a sibling method of a folded class
      const id = `${edge.kind}|${source}|${target}`;
      const merged = edges.get(id);
      if (merged) {
        if (edge.confidence === "exact") merged.confidence = "exact";  // exact if any merged edge is
        continue;
      }
      edges.set(id, {
        id, source, target, kind: edge.kind, confidence: edge.confidence, crossesGroups: groupOf(source) !== groupOf(target),
      });
    }
    for (const node of shown) {
      // No spring from the root package to its modules: those springs drag every module to one spot.
      if (!node.parent || !nodeByKey.get(node.parent).parent) continue;
      const id = `contains|${node.parent}|${node.key}`;
      edges.set(id, {
        id, source: node.parent, target: node.key, kind: "contains",
        color: groupColor.get(groupOf(node.key)), crossesGroups: groupOf(node.parent) !== groupOf(node.key),
      });
    }
    const connections = new Map(shown.map((node) => [node.key, 0]));
    for (const edge of edges.values()) {
      connections.set(edge.source, connections.get(edge.source) + 1);
      connections.set(edge.target, connections.get(edge.target) + 1);
    }
    const nodeElements = shown.map((node) => {
      const count = connections.get(node.key);
      return {
        group: "nodes",
        data: {
          id: node.key, label: shortName(node.key), kind: node.kind, private: isHiddenAsPrivate(node.key),
          connections: count, size: Math.min(56, 10 + 5.5 * Math.sqrt(count)), big: count >= BIG_NODE_DEGREE,
          groupName: groupOf(node.key), image: groupBall.get(groupOf(node.key)), color: groupColor.get(groupOf(node.key)),
          folded: isFolded(node.key, unfolded),
        },
      };
    });
    return [...nodeElements, ...[...edges.values()].map((edgeData) => ({ group: "edges", data: edgeData }))];
  }

  // --- style ------------------------------------------------------------------------------

  const RUNNING_COLOR = "#ffb300";
  const RUNTIME_ONLY_COLOR = "#ff3d3d";  // red: a call that ran but is missing from the static graph
  const CURRENT_STEP_COLOR = "#00e5ff";  // cyan: the player's current step

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
    { selector: "edge[confidence = 'runtime']", style: { "line-color": RUNTIME_ONLY_COLOR, "target-arrow-color": RUNTIME_ONLY_COLOR } },
    // hover and click
    { selector: ".dim", style: { opacity: 0.07 } },
    { selector: "node.lit", style: { opacity: 1, "underlay-opacity": 0.55, "underlay-padding": 8, "min-zoomed-font-size": 0, color: "#e6ebf5", "text-opacity": 1 } },
    { selector: "edge.lit", style: { opacity: 0.95, width: 2 } },
    { selector: "node.chosen", style: { "border-width": 2, "border-color": "#ffffff", "min-zoomed-font-size": 0 } },
    // the player's current step: after hover and click, so a dimmed neighbourhood never hides it
    // (in a Cytoscape style list the later rule wins)
    { selector: "node.current", style: { opacity: 1, "underlay-color": CURRENT_STEP_COLOR, "underlay-opacity": 0.7, "underlay-padding": 9 } },
    { selector: "edge.current", style: { opacity: 1, width: 3.5, "line-color": CURRENT_STEP_COLOR, "target-arrow-color": CURRENT_STEP_COLOR } },
    { selector: ".hidden", style: { display: "none" } },
  ];

  window.graphcode.networkElements = { groupNames, groupColor, networkStyle, buildElements, visibleKey, foldedAncestors, isFolded };
})();
