(() => {
  let overlay;
  let lastFocusedElement;

  function closeDiagram() {
    if (!overlay) return;
    overlay.remove();
    overlay = undefined;
    lastFocusedElement?.focus();
    lastFocusedElement = undefined;
  }

  function openDiagram(svg, trigger) {
    closeDiagram();
    lastFocusedElement = trigger;

    overlay = document.createElement("div");
    overlay.className = "mermaid-zoom-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "放大的 Mermaid 图表");

    const dialog = document.createElement("div");
    dialog.className = "mermaid-zoom-dialog";
    dialog.append(svg.cloneNode(true));

    const closeButton = document.createElement("button");
    closeButton.className = "mermaid-zoom-close";
    closeButton.type = "button";
    closeButton.setAttribute("aria-label", "关闭放大的图表");
    closeButton.textContent = "×";
    closeButton.addEventListener("click", closeDiagram);

    dialog.append(closeButton);
    overlay.append(dialog);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) closeDiagram();
    });
    document.body.append(overlay);
    closeButton.focus();
  }

  document.addEventListener("click", (event) => {
    if (overlay) return;
    const diagram = event.target.closest(".md-typeset .mermaid");
    const svg = diagram?.querySelector("svg");
    if (svg) openDiagram(svg, diagram);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDiagram();
  });

  function labelDiagrams() {
    document.querySelectorAll(".md-typeset .mermaid").forEach((diagram) => {
      if (!diagram.querySelector("svg")) return;
      diagram.tabIndex = 0;
      diagram.setAttribute("role", "button");
      diagram.setAttribute("aria-label", "Mermaid 图表，点击可放大");
      diagram.title = "点击放大图表";
      diagram.onkeydown = (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          const svg = diagram.querySelector("svg");
          if (svg) openDiagram(svg, diagram);
        }
      };
    });
  }

  if (typeof document$ !== "undefined") {
    document$.subscribe(labelDiagrams);
  } else {
    document.addEventListener("DOMContentLoaded", labelDiagrams);
  }

  document.addEventListener("mermaid:rendered", labelDiagrams);
})();
