/* graphcode viewer: drag the handle left of the side panel to change its width.
 *
 * The width is remembered in localStorage (if available) and applies to both
 * modes. Cytoscape watches its container's size, so both views follow live.
 */
(function () {
  "use strict";

  const panel = document.getElementById("panel");
  const handle = document.getElementById("panel-resizer");
  const STORAGE_KEY = "graphcode-panel-width";
  const MIN_WIDTH = 220;  // px; the maximum (75% of the window) is set in viewer.css

  /** Set the panel's width in pixels, kept within the allowed range. */
  function setWidth(width) {
    const maxWidth = window.innerWidth * 0.75;
    panel.style.width = `${Math.round(Math.min(maxWidth, Math.max(MIN_WIDTH, width)))}px`;
  }

  try {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    if (saved) setWidth(saved);
  } catch (error) { /* storage unavailable: default width */ }

  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);  // keep receiving moves even off the handle
    document.body.classList.add("resizing");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!document.body.classList.contains("resizing")) return;
    setWidth(window.innerWidth - event.clientX);  // the panel spans from the pointer to the right edge
  });
  handle.addEventListener("pointerup", () => {
    document.body.classList.remove("resizing");
    try { localStorage.setItem(STORAGE_KEY, String(panel.offsetWidth)); } catch (error) { /* not remembered */ }
  });
})();
