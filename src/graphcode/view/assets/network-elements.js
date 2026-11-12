/* graphcode viewer, Network mode: what to draw (groups, colours, ball pictures, folded classes).
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
          connections: count, size: Math.min(70, 12 + 7 * Math.sqrt(count)), big: count >= BIG_NODE_DEGREE,
          image: groupBall.get(groupOf(node.key)), color: groupColor.get(groupOf(node.key)),
          folded: isFolded(node.key, unfolded),
        },
      };
    });
    return [...nodeElements, ...[...edges.values()].map((edgeData) => ({ group: "edges", data: edgeData }))];
  }

  window.graphcode.networkElements = { groupNames, groupColor, buildElements, visibleKey, foldedAncestors, isFolded };
})();
