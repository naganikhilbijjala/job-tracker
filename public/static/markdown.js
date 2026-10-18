"use strict";

// Small Markdown renderer for AI output: headings, lists, bold/italic, code, links, rules.
// Everything is HTML-escaped first, so model output can never inject markup.
(() => {
  const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const inline = (s) => escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');

  function renderMarkdown(src) {
    const out = [];
    let list = null;   // "ul" | "ol"
    let para = [];
    const flushPara = () => { if (para.length) out.push(`<p>${para.join("<br>")}</p>`); para = []; };
    const closeList = () => { if (list) out.push(`</${list}>`); list = null; };
    const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^```(\w*)/);
      if (fence) {
        flushPara(); closeList();
        const code = [];
        for (i++; i < lines.length && !lines[i].startsWith("```"); i++) code.push(lines[i]);
        out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)/);
      const bullet = line.match(/^\s*[-*•]\s+(.*)/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (heading) {
        flushPara(); closeList();
        out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`);
      } else if (bullet || numbered) {
        flushPara();
        const kind = bullet ? "ul" : "ol";
        if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushPara(); closeList(); out.push("<hr>");
      } else if (!line.trim()) {
        flushPara(); closeList();
      } else {
        closeList();
        para.push(inline(line.trim()));
      }
    }
    flushPara(); closeList();
    return out.join("\n");
  }

  window.renderMarkdown = renderMarkdown;
  window.escapeHtml = escapeHtml;
})();
