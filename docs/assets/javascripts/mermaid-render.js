(() => {
  function currentTheme() {
    return document.body.getAttribute("data-md-color-scheme") === "slate" ? "dark" : "default";
  }

  function configureMermaid() {
    if (!window.mermaid) return false;
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: currentTheme(),
      flowchart: { useMaxWidth: true },
    });
    return true;
  }

  // This runs while the document is still loading, before Mermaid's default
  // DOMContentLoaded handler can consume the source blocks.
  configureMermaid();

  async function renderDiagrams() {
    if (!configureMermaid()) return;
    const selector = ".md-typeset pre.mermaid-source:not([data-processed])";
    const diagrams = [...document.querySelectorAll(selector)];
    if (!diagrams.length) return;
    try {
      await Promise.all(
        diagrams.map(async (diagram, index) => {
          const source = diagram.textContent.trim();
          const id = `mermaid-${Date.now()}-${index}`;
          const { svg, bindFunctions } = await window.mermaid.render(id, source);
          diagram.innerHTML = svg;
          diagram.classList.add("mermaid");
          diagram.setAttribute("data-processed", "true");
          bindFunctions?.(diagram);
        }),
      );
      document.dispatchEvent(new Event("mermaid:rendered"));
    } catch (error) {
      const detail = error?.message ?? error?.str ?? String(error);
      console.error("Unable to render Mermaid diagram", detail);
      document.querySelectorAll(selector).forEach((node) => node.setAttribute("data-mermaid-error", detail));
    }
  }

  if (typeof document$ !== "undefined") {
    document$.subscribe(renderDiagrams);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderDiagrams);
  } else {
    renderDiagrams();
  }
})();
