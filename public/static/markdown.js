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

  // ---- resume layout: "Left | Right" rows, with dates pushed to the right edge
  const DATEISH = /\b(19|20)\d{2}\b|\bpresent\b|\bcurrent\b|\bexpected\b/i;

  function splitRow(text, rightIfNoDate) {
    const parts = text.split(/\s+\|\s+/);
    if (parts.length < 2) return [text, ""];
    let i = parts.findIndex((p, k) => k > 0 && DATEISH.test(p));
    if (i === -1 && rightIfNoDate) i = parts.length - 1;
    if (i === -1) return [text, ""];
    const right = parts.splice(i, 1)[0];
    return [parts.join(" | "), right];
  }

  const row = (cls, [left, right]) =>
    `<div class="${cls}"><span class="l">${inline(left)}</span>${right ? `<span class="r">${inline(right)}</span>` : ""}</div>`;

  // Expects the structure the assistant is asked for:
  //   # Name / contact line / ## Section / ### Company | Location / **Title** | Dates / - bullets
  // and degrades gracefully for anything else.
  function renderResume(src) {
    const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let section = "", inEntry = false, list = false, afterHead = false, para = [];
    const flushPara = () => { if (para.length) out.push(`<p>${para.join("<br>")}</p>`); para = []; };
    const closeList = () => { if (list) out.push("</ul>"); list = false; };
    const closeEntry = () => { flushPara(); closeList(); if (inEntry) out.push("</section>"); inEntry = false; };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      let m;
      if ((m = line.match(/^#\s+(.*)/))) {
        closeEntry();
        const contact = [];
        while (i + 1 < lines.length && lines[i + 1].trim() && !lines[i + 1].trim().startsWith("#")) {
          contact.push(...lines[++i].split(/\s+[|·•]\s+/).map((c) => c.trim()).filter(Boolean));
        }
        out.push(`<header><h1>${inline(m[1])}</h1>${contact.length ? `<p class="contact">${contact
          .map((c) => `<span class="item">${inline(c)}</span>`).join('<span class="sep"> | </span>')}</p>` : ""}</header>`);
      } else if ((m = line.match(/^##\s+(.*)/))) {
        closeEntry();
        section = m[1].toLowerCase();
        out.push(`<h2>${inline(m[1])}</h2>`);
      } else if ((m = line.match(/^###\s+(.*)/))) {
        closeEntry();
        out.push('<section class="entry">', row("entry-head", splitRow(m[1], true)));
        inEntry = afterHead = true;
        continue;
      } else if ((m = line.match(/^[-*•]\s+(.*)/))) {
        flushPara();
        const skills = section.includes("skill");
        if (!list) { out.push(`<ul${skills ? ' class="skills"' : ""}>`); list = true; }
        let html = inline(m[1]);
        if (skills && !html.startsWith("<strong>")) html = html.replace(/^([^:<]{2,40}):\s*/, "<strong>$1:</strong> ");
        out.push(`<li>${html}</li>`);
      } else if (!line) {
        flushPara(); closeList();
      } else if (afterHead && !list) {
        out.push(row("entry-sub", splitRow(line, false)));
      } else {
        closeList();
        para.push(inline(line));
      }
      afterHead = false;
    }
    closeEntry();
    return out.join("\n");
  }

  window.renderResume = renderResume;
  window.renderMarkdown = renderMarkdown;
  window.escapeHtml = escapeHtml;
})();
