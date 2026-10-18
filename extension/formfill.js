// Injected into an application form by the popup. Reads the form's questions and fills in answers.
// The popup calls window.__jtForm.scan() and .fill(answers); this file only sets them up.
(() => {
  if (window.__jtForm) return;

  const COLORS = { profile: "#1f9d55", saved: "#1f9d55", ai: "#8b5cf6" };
  const TIPS = {
    profile: "Filled from your Job Tracker profile",
    saved: "Filled from an answer you saved",
    ai: "Written by AI from your profile. Check it before you submit",
  };
  const PLACEHOLDER = /^(select|choose|please select|--|—|-)\b|^\s*$/i;
  let fields = new Map();  // id -> { kind, el, els, outline }

  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) &&
    getComputedStyle(el).visibility !== "hidden";
  const text = (el) => (el ? (el.innerText || el.textContent || "") : "").replace(/\s+/g, " ").trim();

  // LinkedIn repeats a label for screen readers ("Phone Phone"); keep one copy.
  function dedupe(t) {
    const half = t.length / 2;
    if (Number.isInteger(half - 0.5) && t.slice(0, half - 0.5) === t.slice(half + 0.5)) return t.slice(0, half - 0.5);
    return t;
  }
  const tidy = (t) => dedupe(String(t || "").replace(/\s+/g, " ").trim()).replace(/\s*\*\s*$/, "").trim();

  // The form in front: an open dialog with fields (LinkedIn Easy Apply), else the page.
  function scope() {
    const dialogs = [...document.querySelectorAll('[role="dialog"], dialog[open], .artdeco-modal')]
      .filter((d) => visible(d) && d.querySelector("input, textarea, select"));
    return dialogs.at(-1) || document;
  }

  function labelOf(el) {
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => text(document.getElementById(id))).filter(Boolean).join(" ");
      if (t) return tidy(t);
    }
    const own = [...(el.labels || [])].map(text).filter(Boolean)[0];
    if (own) return tidy(own);
    if (el.getAttribute("aria-label")) return tidy(el.getAttribute("aria-label"));
    // A label-ish element just above the field, in the same small container.
    let node = el;
    for (let i = 0; i < 4 && node.parentElement; i++) {
      node = node.parentElement;
      const lbl = [...node.querySelectorAll("label, legend, [class*='label' i], [class*='question' i]")]
        .find((l) => !l.contains(el) && text(l));
      if (lbl) return tidy(text(lbl));
    }
    return tidy(el.placeholder || el.name || "");
  }

  // The question for a group of radios or checkboxes.
  function groupLabel(inputs) {
    const first = inputs[0];
    const group = first.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (group) {
      const by = group.getAttribute("aria-labelledby");
      const t = by ? by.split(/\s+/).map((id) => text(document.getElementById(id))).join(" ")
        : text(group.querySelector("legend")) || group.getAttribute("aria-label");
      if (t) return tidy(t);
    }
    // Otherwise the nearest text above the options that isn't one of them.
    let node = first.parentElement;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      if (inputs.every((x) => node.contains(x))) {
        const lbl = [...node.querySelectorAll("label, legend, span, p, div")]
          .find((l) => !inputs.some((x) => l.contains(x) || (x.labels && [...x.labels].includes(l))) && text(l) && !l.querySelector("input"));
        if (lbl) return tidy(text(lbl));
      }
    }
    return "";
  }

  const optionLabel = (input) => tidy([...(input.labels || [])].map(text).join(" ") || input.value);

  function kindOf(el) {
    if (el.tagName === "TEXTAREA") return "textarea";
    if (el.tagName === "SELECT") return "select";
    const t = (el.type || "text").toLowerCase();
    if (["text", "search", ""].includes(t)) return "text";
    return ["email", "tel", "url", "number", "date", "radio", "checkbox"].includes(t) ? t : null;
  }

  function scan() {
    fields = new Map();
    const out = [];
    const root = scope();
    const add = (entry, info) => {
      const id = `f${out.length + 1}`;
      fields.set(id, entry);
      out.push({ id, ...info });
    };
    const groups = new Map();
    for (const el of root.querySelectorAll("input, textarea, select")) {
      if (el.disabled || el.readOnly || !visible(el) && !["radio", "checkbox"].includes(el.type)) continue;
      const kind = kindOf(el);
      if (!kind) continue;
      if (kind === "radio" || kind === "checkbox") {
        if (!visible(el) && !visible(el.parentElement)) continue;
        const key = (el.name && `${kind}:${el.name}`) || el.closest("fieldset, [role='radiogroup'], [role='group']") || el;
        if (!groups.has(key)) groups.set(key, { kind, els: [] });
        groups.get(key).els.push(el);
        continue;
      }
      const label = labelOf(el);
      if (!label) continue;
      if (kind === "select") {
        const options = [...el.options].filter((o) => o.value && !PLACEHOLDER.test(o.text)).map((o) => o.text.trim());
        const current = el.selectedOptions[0];
        const empty = !el.value || !current || PLACEHOLDER.test(current.text);
        add({ kind, el }, { label, kind, options, empty, value: empty ? "" : current.text.trim() });
      } else {
        add({ kind, el }, { label, kind, options: [], empty: !el.value.trim(), value: el.value.trim() });
      }
    }
    for (const { kind, els } of groups.values()) {
      // A lone checkbox is a consent or "I agree": always left to the person.
      if (kind === "checkbox" && els.length < 2) continue;
      const label = groupLabel(els);
      if (!label) continue;
      const checked = els.filter((x) => x.checked).map(optionLabel);
      add({ kind, els }, { label, kind, options: els.map(optionLabel), empty: !checked.length, value: checked.join("; ") });
    }
    return out;
  }

  // Set a value the way typing does, so React and other frameworks notice it.
  function setValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function mark(target, source) {
    const before = { outline: target.style.outline, offset: target.style.outlineOffset, title: target.title };
    target.style.outline = `2px solid ${COLORS[source]}`;
    target.style.outlineOffset = "2px";
    target.title = TIPS[source];
    // Once you've looked at it (clicked into it), the highlight goes.
    const clear = () => {
      target.style.outline = before.outline;
      target.style.outlineOffset = before.offset;
      target.title = before.title;
    };
    target.addEventListener("focusin", clear, { once: true });
  }

  const same = (a, b) => a.trim().toLowerCase() === b.trim().toLowerCase();

  function fill(answers) {
    const results = [];
    for (const { id, value, source } of answers) {
      const f = fields.get(id);
      if (!f) { results.push({ id, ok: false }); continue; }
      try {
        if (f.kind === "radio") {
          const pick = f.els.find((x) => same(optionLabel(x), value));
          if (!pick) throw new Error("no matching option");
          if (!pick.checked) pick.click();
          mark(f.els[0].closest("fieldset, [role='radiogroup']") || pick.parentElement, source);
        } else if (f.kind === "checkbox") {
          const wanted = value.split(/\s*;\s*/);
          const picks = f.els.filter((x) => wanted.some((w) => same(optionLabel(x), w)));
          if (!picks.length) throw new Error("no matching option");
          picks.forEach((x) => !x.checked && x.click());
          mark(f.els[0].closest("fieldset, [role='group']") || picks[0].parentElement, source);
        } else if (f.kind === "select") {
          const opt = [...f.el.options].find((o) => same(o.text, value));
          if (!opt) throw new Error("no matching option");
          setValue(f.el, opt.value);
          mark(f.el, source);
        } else {
          setValue(f.el, value);
          mark(f.el, source);
        }
        results.push({ id, ok: true });
      } catch {
        results.push({ id, ok: false });
      }
    }
    return results;
  }

  window.__jtForm = { scan, fill };
})();
