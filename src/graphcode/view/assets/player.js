/* graphcode viewer: step through a trace, one call at a time, in execution order.
 *
 * Controls above the call tree: first, previous, play/pause, next. Each step
 * lights its function and the call that led to it (in whichever mode is shown,
 * through the stepHooks registered by trace.js and network.js), highlights its
 * row in the call tree and shows its code in the details panel. Clicking a row
 * jumps to that step; viewer.html#trace=<name>&step=<n> opens at step n.
 *
 * Listens to the "graphcode:tracechange" event that trace.js sends.
 */
(function () {
  "use strict";

  const { data, element, showDetails, nodeByKey, stepHooks } = window.graphcode;
  const STEP_DELAY_MS = 900;  // pause between two steps while playing

  let steps = [];
  let current = -1;  // index of the current step; -1 before the first
  let timer = null;  // set while playing
  let status = null;  // the "Step 3 / 22 · DAG.add_edge" line
  let playButton = null;

  /** Make step `index` the current one: light it, highlight its row, show its code. */
  function goTo(index) {
    if (steps.length === 0) return;
    current = Math.min(steps.length - 1, Math.max(0, index));
    const step = steps[current];
    const callerKey = step.parent === null ? null : steps[step.parent].key;
    for (const lightStep of stepHooks) lightStep(step.key, callerKey);
    const panel = document.getElementById("trace-panel");
    panel.querySelectorAll("li.current-step").forEach((row) => row.classList.remove("current-step"));
    const row = panel.querySelector(`li[data-step="${step.id}"]`);
    if (row) {
      row.classList.add("current-step");
      row.scrollIntoView({ block: "nearest" });
    }
    if (nodeByKey.has(step.key)) showDetails(step.key);
    const name = step.key.includes(":") ? step.key.split(":")[1] : step.key;
    status.textContent = `Step ${current + 1} / ${steps.length} · ${name}`;
  }

  /** Stop playing (if playing). */
  function pause() {
    if (timer !== null) clearInterval(timer);
    timer = null;
    if (playButton) playButton.textContent = "▶ Play";
  }

  /** Play from the current step (from the start if at the end), one step per STEP_DELAY_MS. */
  function play() {
    if (current >= steps.length - 1) current = -1;
    playButton.textContent = "⏸ Pause";
    goTo(current + 1);
    timer = setInterval(() => {
      if (current >= steps.length - 1) pause();
      else goTo(current + 1);
    }, STEP_DELAY_MS);
  }

  /** A small button with a tooltip, running `action` when clicked. */
  function button(label, title, action) {
    const created = element("button", label);
    created.type = "button";
    created.title = title;
    created.addEventListener("click", action);
    return created;
  }

  /** Put the controls above the call tree of the trace just shown. */
  function addControls() {
    const panel = document.getElementById("trace-panel");
    const controls = element("div", undefined, "player");
    playButton = button("▶ Play", "Play the steps in order", () => (timer === null ? play() : pause()));
    controls.append(
      button("⏮", "First step", () => { pause(); goTo(0); }),
      button("◀", "Previous step", () => { pause(); goTo(current - 1); }),
      playButton,
      button("▶", "Next step", () => { pause(); goTo(current + 1); }),
    );
    status = element("span", `${steps.length} steps: press Play or click a row`, "player-status");
    controls.append(status);
    panel.querySelector("ol.call-tree").before(controls);
  }

  /** Start over with a newly shown trace (or none). */
  function onTraceChange(trace) {
    pause();
    steps = trace ? trace.steps : [];
    current = -1;
    for (const lightStep of stepHooks) lightStep(null, null);
    if (trace && steps.length > 0) addControls();
  }

  document.addEventListener("graphcode:tracechange", (event) => onTraceChange(event.detail));
  // A trace opened from the URL (#trace=...) was shown before this file loaded: pick it up now.
  const menu = document.getElementById("trace-select");
  if (menu.value !== "") onTraceChange(data.traces[Number(menu.value)]);
  // viewer.html#trace=<name>&step=<n> opens at step n (counted from 1, as shown in the panel).
  const stepParameter = location.hash.slice(1).split("&").find((part) => part.startsWith("step="));
  if (stepParameter && steps.length > 0) goTo(Number(stepParameter.slice("step=".length)) - 1);

  // A click on a call-tree row makes it the current step.
  document.getElementById("trace-panel").addEventListener("click", (event) => {
    const row = event.target.closest("li[data-step]");
    if (!row) return;
    pause();
    goTo(Number(row.dataset.step));
  });

  // After switching mode, light the current step again in the mode now shown.
  for (const id of ["mode-classic", "mode-network"]) {
    document.getElementById(id)?.addEventListener("click", () => { if (current >= 0) goTo(current); });
  }
})();
