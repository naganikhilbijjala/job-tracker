"use strict";
// The popup's "Application form" box: fills the form on the page from the tracker, and remembers
// answers you typed so the next form fills itself. Uses api(), show(), config and form from popup.js.

const applyBox = document.getElementById("apply");
const applyMsg = document.getElementById("apply-msg");
const fillBtn = document.getElementById("fill");
const rememberBtn = document.getElementById("remember");
const rememberList = document.getElementById("remember-list");
let applyTab = null;
let candidates = [];

const escHTML = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const postJSON = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

async function inPage(func, args = []) {
  const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: applyTab.id }, func, args });
  return result;
}

async function scanPage() {
  await chrome.scripting.executeScript({ target: { tabId: applyTab.id }, files: ["formfill.js"] });
  return inPage(() => window.__jtForm.scan());
}

// The job this form is for, as the popup read it from the page.
const pageJob = () => ({
  url: form.url.value, title: form.title.value, company: form.company.value, description: form.description.value,
});

function applyError(err) {
  show(applyMsg, err instanceof AuthError ? "Your login expired. Open Settings and connect again." : escHTML(err.message), "err");
}

async function initApply() {
  const { server } = await chrome.storage.local.get({ server: "" });
  if (!server) return;
  [applyTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let found;
  try {
    found = await scanPage();
  } catch {
    return;  // a page extensions can't touch (chrome://, the Web Store, PDFs)
  }
  if (!found?.length) return;
  const empty = found.filter((f) => f.empty).length;
  document.getElementById("apply-count").textContent = `${plural(found.length, "question")} · ${empty} empty`;
  applyBox.hidden = false;
}

fillBtn.addEventListener("click", async () => {
  fillBtn.disabled = true;
  rememberList.hidden = true;
  show(applyMsg, "Reading the form…");
  try {
    const fields = (await scanPage()).filter((f) => f.empty);
    if (!fields.length) return show(applyMsg, "Every question on this page already has an answer.");
    show(applyMsg, "Finding answers… (the AI can take a few seconds)");
    const res = await postJSON("/api/autofill", { fields, job: pageJob() });
    const results = await inPage((answers) => window.__jtForm.fill(answers), [res.answers]);
    const ok = new Set(results.filter((r) => r.ok).map((r) => r.id));
    const filled = res.answers.filter((a) => ok.has(a.id));
    const byAI = filled.filter((a) => a.source === "ai").length;
    const labelOf = (id) => fields.find((f) => f.id === id)?.label || "";
    const left = [...res.left.map((l) => l.label), ...res.answers.filter((a) => !ok.has(a.id)).map((a) => labelOf(a.id))];

    const parts = [filled.length ? `<b style="color:var(--ok)">Filled ${plural(filled.length, "question")}.</b>` : "<b>Nothing filled yet.</b>"];
    if (filled.length) {
      parts.push(`<div class="legend"><span><i style="background:#1f9d55"></i>Your profile or saved answers</span>` +
        (byAI ? `<span><i style="background:#8b5cf6"></i>AI: check these</span>` : "") + "</div>");
    }
    if (res.ai_error) parts.push(`<div style="margin-top:6px">${escHTML(res.ai_error)}</div>`);
    if (left.length) {
      parts.push(`<div style="margin-top:6px">Left for you (${left.length}):</div><ul>` +
        left.slice(0, 6).map((l) => `<li>${escHTML(l)}</li>`).join("") + (left.length > 6 ? "<li>…</li>" : "") + "</ul>" +
        `<div class="hint">Answer them, then click <b>Remember my answers</b> and they'll fill in next time.</div>`);
    }
    parts.push(`<div class="hint">Nothing is submitted for you. Review the page, then continue.</div>`);
    show(applyMsg, parts.join(""));
  } catch (err) {
    applyError(err);
  } finally {
    fillBtn.disabled = false;
  }
});

rememberBtn.addEventListener("click", async () => {
  rememberBtn.disabled = true;
  try {
    const fields = (await scanPage()).filter((f) => !f.empty);
    ({ candidates } = await postJSON("/api/answers/collect", { fields, company: form.company.value }));
    if (!candidates.length) {
      rememberList.hidden = true;
      return show(applyMsg, "Nothing new to remember on this page. Answers about this one company aren't saved.");
    }
    applyMsg.hidden = true;
    // Long written answers are usually about one job, so they start unticked.
    rememberList.innerHTML = `<div class="hint" style="margin:0 0 6px">Save these for the next form?</div>` +
      candidates.map((c, i) => `<label><input type="checkbox" data-i="${i}" ${c.kind === "textarea" ? "" : "checked"}>
        <span>${escHTML(c.question)}<small>${c.status === "changed" ? "Replaces your saved answer: " : ""}${escHTML(c.answer)}</small></span></label>`).join("") +
      `<div class="row"><button type="button" id="save-answers" class="primary">Save answers</button></div>`;
    rememberList.hidden = false;
  } catch (err) {
    applyError(err);
  } finally {
    rememberBtn.disabled = false;
  }
});

rememberList.addEventListener("click", async (e) => {
  if (e.target.id !== "save-answers") return;
  const items = [...rememberList.querySelectorAll("input:checked")].map((x) => candidates[Number(x.dataset.i)])
    .map(({ question, answer }) => ({ question, answer }));
  if (!items.length) return show(applyMsg, "Tick the answers to save.");
  e.target.disabled = true;
  try {
    const { saved } = await postJSON("/api/answers", { items });
    rememberList.hidden = true;
    show(applyMsg, `Saved ${plural(saved, "answer")} ✓ They'll fill in on the next form. Edit them on ApplyStack's Profile tab.`, "ok");
  } catch (err) {
    e.target.disabled = false;
    applyError(err);
  }
});

initApply();
