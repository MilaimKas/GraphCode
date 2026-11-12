/* graphcode viewer: trace overlay, call-tree panel and staleness banner.
 *
 * Picking a trace in the toolbar menu:
 * - nodes that ran are outlined in amber, everything else fades;
 * - calls that ran are drawn thick in amber; calls that ran but are missing
 *   from the static graph are added as new "runtime" edges, in red;
 * - boxes are opened exactly where something ran, closed elsewhere;
 * - the panel lists the call tree (player.js adds the step-through controls).
 *
 * Uses what viewer.js shares on window.graphcode. Announces each change of trace
 * with a "graphcode:tracechange" event on document (detail: the trace, or null).
 */
(function () {
  "use strict";

  const { data, cy, expandCollapse, nodeByKey, detailsHooks, stepHooks } = window.graphcode;
  const { element, collapseDefault } = window.graphcode;
  const RAN_COLOR = "#f59e0b";  // amber, as in Network mode
  const RUNTIME_ONLY_COLOR = "#d50000";  // red: a call the static graph missed
  const CURRENT_STEP_COLOR = "#1565c0";  // blue: the player's current step
  const traces = data.traces;
  const MAX_TREE_ROWS = 3000;  // a longer call tree is cut in the panel (the graph shows everything)

  cy.style()
    .selector("node.ran").style({ "border-width": 4, "border-color": RAN_COLOR })
    .selector("edge.ran").style({ width: 4, opacity: 1, "line-color": RAN_COLOR, "target-arrow-color": RAN_COLOR })
    .selector("edge[confidence = 'runtime']")
    .style({ "line-color": RUNTIME_ONLY_COLOR, "target-arrow-color": RUNTIME_ONLY_COLOR, "line-style": "solid", width: 4.5 })
    .selector(".untraced").style({ opacity: 0.12 })
    .selector("node.selected").style({ opacity: 1 })  // a selected node stays visible even if it did not run
    .selector("node.current").style({ "border-width": 7, "border-color": CURRENT_STEP_COLOR, opacity: 1 })
    .selector("edge.current")
    .style({ width: 7, "line-color": CURRENT_STEP_COLOR, "target-arrow-color": CURRENT_STEP_COLOR, opacity: 1, "z-index": 10 })
    .update();

  /**
   * Whether a step's function is in the graph shown now. Not the trace's own
   * `matched`: that was decided against the graph at recording time, and a
   * function renamed or deleted since then has no node to draw.
   */
  const inGraph = (key) => nodeByKey.has(key);

  let callsByKey = new Map();  // key -> number of calls in the current trace

  // --- the overlay ------------------------------------------------------------

  /** Show `trace` on the graph, or remove any trace overlay when it is null. */
  function showTrace(trace) {
    // Open everything first: edges can only be added between nodes that are drawn.
    expandCollapse.expandAll({ layoutBy: null });
    cy.remove(cy.edges("[confidence = 'runtime']"));
    cy.elements().removeClass("ran untraced faded selected");
    document.getElementById("details").replaceChildren(
      element("p", "Click a node to see its code and neighbours. Double-click a box to expand or collapse it.", "hint"),
    );
    document.body.classList.toggle("tracing", trace !== null);
    callsByKey = new Map();
    if (trace === null) {
      document.getElementById("trace-panel").hidden = true;
      document.getElementById("trace-banner").hidden = true;
      collapseDefault();
      announce(null);
      return;
    }
    const steps = trace.steps;
    const stepCalls = totalCalls(steps);
    const ranKeys = new Set();
    for (const step of steps) {
      if (!inGraph(step.key)) continue;
      ranKeys.add(step.key);
      callsByKey.set(step.key, (callsByKey.get(step.key) || 0) + stepCalls[step.id]);
    }
    const ranElements = markCalls(steps, ranKeys);
    collapseDefault((box) => box.descendants().some((node) => ranKeys.has(node.id())));
    // After collapsing (which redraws edges onto closed boxes): fade what did not
    // run, but never a box holding something that ran, since a box's
    // transparency also applies to its contents.
    const keep = ranElements.union(ranElements.nodes().ancestors());
    cy.elements().not(keep).addClass("untraced");
    showCallTree(trace, stepCalls);
    showBanner(trace);
    announce(trace);
  }

  /** Tell the player (and anything else listening) that the shown trace changed. */
  function announce(trace) {
    document.dispatchEvent(new CustomEvent("graphcode:tracechange", { detail: trace }));
  }

  /** Light the player's current step in Classic mode: the function, and the call that led to it. */
  stepHooks.push((key, callerKey) => {
    cy.elements().removeClass("current");
    if (key === null || document.body.dataset.mode === "network") return;
    const node = cy.getElementById(key);
    if (node.empty()) return;  // not in the graph, or inside a box closed by hand
    node.addClass("current");
    if (callerKey) cy.getElementById(`calls|${callerKey}|${key}`).union(cy.getElementById(`runtime|${callerKey}|${key}`)).addClass("current");
    const shown = cy.extent();
    const { x, y } = node.position();
    if (x < shown.x1 || x > shown.x2 || y < shown.y1 || y > shown.y2) cy.animate({ center: { eles: node } }, { duration: 300 });
  });

  /** Calls per step: a step x2 under a parent x3 ran 6 times (steps are parents first). */
  function totalCalls(steps) {
    const totals = [];
    for (const step of steps) totals[step.id] = step.count * (step.parent === null ? 1 : totals[step.parent]);
    return totals;
  }

  /** Outline the nodes and static edges that ran; add runtime-only edges; return them all. */
  function markCalls(steps, ranKeys) {
    const ranIds = new Set(ranKeys);  // ids, turned into one collection at the end (fast on big traces)
    for (const step of steps) {
      if (step.parent === null || !inGraph(step.key)) continue;
      const caller = steps[step.parent];
      if (!inGraph(caller.key)) continue;
      const staticId = `calls|${caller.key}|${step.key}`;
      const runtimeId = `runtime|${caller.key}|${step.key}`;
      if (cy.getElementById(staticId).nonempty()) {
        ranIds.add(staticId);
      } else if (!ranIds.has(runtimeId)) {
        cy.add({
          group: "edges",
          data: { id: runtimeId, source: caller.key, target: step.key, kind: "calls", confidence: "runtime" },
        });
        ranIds.add(runtimeId);
      }
    }
    const ran = cy.collection([...ranIds].map((id) => cy.getElementById(id)));
    ran.addClass("ran");
    return ran;
  }

  // --- the panel and the banner -------------------------------------------------------

  /** The call tree as an indented list; a click shows the step's node. */
  function showCallTree(trace, stepCalls) {
    const panel = document.getElementById("trace-panel");
    const header = trace.header;
    panel.replaceChildren(element("h3", `Call tree · ${header.scenario}`));
    panel.append(element("p", `${header.target} · ${header.step_count} steps · recorded ${header.date}`, "meta"));
    if (header.error) panel.append(element("p", `The scenario raised ${header.error}`, "trace-error"));
    if (header.truncated) panel.append(element("p", "Truncated: the step cap stopped the recording.", "trace-error"));
    if (trace.steps.length === 0) panel.append(element("p", "No package function ran.", "hint"));
    const list = element("ol", undefined, "call-tree");
    for (const step of trace.steps.slice(0, MAX_TREE_ROWS)) list.append(treeRow(step, stepCalls[step.id]));
    panel.append(list);
    if (trace.steps.length > MAX_TREE_ROWS) {
      panel.append(element("p", `First ${MAX_TREE_ROWS} of ${trace.steps.length} steps shown.`, "hint"));
    }
    panel.hidden = false;
  }

  /** One line of the call tree: the qualified name (full, so nested functions read clearly), count, outcome. */
  function treeRow(step, calls) {
    const row = element("li");
    row.dataset.step = String(step.id);  // a click makes it the player's current step (player.js)
    row.style.paddingLeft = `${step.depth * 14}px`;
    const name = step.key.includes(":") ? step.key.split(":")[1] : step.key;
    const shown = inGraph(step.key);
    row.append(element("a", name, shown ? "" : "unmatched"));
    if (step.count > 1) row.append(element("span", ` ×${step.count}`, "count"));
    if (step.outcome === "raise") row.append(element("span", ` raise ${step.exception}`, "trace-error"));
    if (step.outcome === "unfinished") row.append(element("span", " unfinished", "hint"));
    if (!shown) row.append(element("span", " (not in the graph)", "hint"));
    row.title = `${step.key}\n${calls} call(s) in this trace`;
    return row;
  }

  /** Warn when the trace and the graph may describe different code. */
  function showBanner(trace) {
    const banner = document.getElementById("trace-banner");
    const traced = trace.header;
    const built = data.graph.header;
    const reasons = [];
    if (traced.commit !== built.commit) {
      reasons.push(`the trace was recorded on commit ${shortCommit(traced.commit)}, the graph built on ${shortCommit(built.commit)}`);
    }
    if (traced.dirty) reasons.push("the trace was recorded with uncommitted changes");
    if (built.dirty) reasons.push("the graph was built with uncommitted changes");
    banner.textContent = reasons.length ? `⚠ The trace and the graph may disagree: ${reasons.join("; ")}.` : "";
    banner.hidden = reasons.length === 0;
  }

  /** First 7 characters of a commit hash, as git shows them. */
  function shortCommit(commit) {
    return commit ? commit.slice(0, 7) : "none (not in git)";
  }

  /** In the details panel: how often the node ran in the current trace. */
  detailsHooks.push((key, panel) => {
    if (!document.body.classList.contains("tracing")) return;
    const calls = callsByKey.get(key) || 0;
    const text = calls ? `Ran ${calls} time${calls > 1 ? "s" : ""} in this trace.` : "Did not run in this trace.";
    panel.insertBefore(element("p", text, "trace-calls"), panel.children[2] || null);
  });

  // --- the menu -------------------------------------------------------------------------

  const menu = document.getElementById("trace-select");
  if (traces.length === 0) {
    document.getElementById("trace-group").hidden = true;
  } else {
    traces.forEach((trace, index) => menu.append(new Option(trace.header.scenario, String(index))));
    menu.addEventListener("change", () => showTrace(menu.value === "" ? null : traces[Number(menu.value)]));
    // viewer.html#trace=<scenario> opens with that trace shown (other parts, e.g. "mode=network&", are skipped).
    const traceParameter = location.hash.slice(1).split("&").find((part) => part.startsWith("trace="));
    const wanted = traceParameter ? decodeURIComponent(traceParameter.slice("trace=".length)) : "";
    const index = traces.findIndex((trace) => trace.header.scenario === wanted);
    if (index >= 0) {
      menu.value = String(index);
      showTrace(traces[index]);
    }
  }
})();
