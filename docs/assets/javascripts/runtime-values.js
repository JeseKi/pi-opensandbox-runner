(() => {
  const managerOriginPlaceholder = "__MANAGER_ORIGIN__";

  function applyRuntimeValues() {
    const content = document.querySelector('[data-md-component="content"]');
    if (!content) return;

    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    const matches = [];
    while (walker.nextNode()) {
      if (walker.currentNode.nodeValue.includes(managerOriginPlaceholder)) {
        matches.push(walker.currentNode);
      }
    }
    for (const node of matches) {
      node.nodeValue = node.nodeValue.replaceAll(
        managerOriginPlaceholder,
        window.location.origin,
      );
    }
  }

  if (typeof document$ !== "undefined") {
    document$.subscribe(applyRuntimeValues);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applyRuntimeValues);
  } else {
    applyRuntimeValues();
  }
})();
