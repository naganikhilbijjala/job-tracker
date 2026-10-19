"use strict";

const form = document.getElementById("f");
const setup = document.getElementById("setup");
const msg = document.getElementById("msg");
const saveBtn = document.getElementById("save");
let status = "applied";
let config = { server: "", token: "" };
const DEFAULT_SERVER = "https://www.applystack.app";

function show(el, text, kind = "") {
  el.hidden = false;
  el.className = "msg " + kind;
  el.innerHTML = text;
}

function setStatus(s) {
  status = s;
  document.querySelectorAll("#statuses button").forEach((b) => b.classList.toggle("on", b.dataset.status === s));
}

class AuthError extends Error {}

async function api(path, opts = {}) {
  const headers = { "X-Today": new Date().toLocaleDateString("sv"), ...(opts.headers || {}) };
  if (config.token) headers.Authorization = `Bearer ${config.token}`;
  const res = await fetch(config.server + path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) throw new AuthError(data.error || "Please log in again");
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function showSetup(note = "", kind = "") {
  form.hidden = true;
  setup.hidden = false;
  setup.server.value = config.server || DEFAULT_SERVER;
  const el = document.getElementById("setup-msg");
  if (note) show(el, note, kind); else el.hidden = true;
}

setup.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("connect");
  let origin;
  try {
    origin = new URL(setup.server.value.trim().replace(/^(?!https?:\/\/)/, "https://")).origin;
  } catch {
    return showSetup("That doesn't look like a web address.", "err");
  }
  btn.disabled = true;
  try {
    // Custom domains need permission; applystack.app, *.vercel.app and localhost are granted at install.
    // (The request must run before any other await, while the click still counts as a user gesture.)
    const preGranted = /^https:\/\/(www\.)?applystack\.app$|^https:\/\/[^/]+\.vercel\.app$|^http:\/\/(127\.0\.0\.1|localhost):8765$/.test(origin);
    const granted = preGranted || await chrome.permissions.request({ origins: [origin + "/*"] });
    if (!granted) throw new Error("The extension needs permission to talk to that address.");
    config = { server: origin, token: "" };
    const meta = await api("/api/meta");
    if (meta.auth?.required) {
      const { token } = await api("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: setup.password.value }),
      });
      config.token = token;
    }
    await chrome.storage.local.set({ server: config.server, token: config.token });
    setup.password.value = "";
    setup.hidden = true;
    init();
  } catch (err) {
    showSetup(err instanceof TypeError ? `Couldn't reach ${origin}. Is the address right?` : err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

function fillForm(job, onlyBlanks = false) {
  for (const [k, v] of Object.entries(job || {})) {
    if (form[k] && v && (!onlyBlanks || !form[k].value)) form[k].value = v;
  }
  const n = form.description.value.length;
  document.getElementById("desc-summary").textContent =
    n ? `Job description (${n.toLocaleString()} chars captured)` : "Job description (none found — paste or highlight it)";
}

function htmlToText(el) {
  if (!el) return "";
  const html = el.innerHTML
    .replace(/<li[^>]*>/gi, "\n• ")
    .replace(/<\/?(br|p|div|h\d|ul|ol)\b[^>]*>|<\/li>/gi, "\n");
  const text = new DOMParser().parseFromString(html, "text/html").documentElement.textContent;
  return text.split("\n").map((l) => l.replace(/\s+/g, " ").trim()).join("\n")
    .replace(/•\n+/g, "• ").replace(/^• *$/gm, "").replace(/\n{3,}/g, "\n\n").trim();
}

async function fetchLinkedIn(url) {
  const m = (url || "").match(/linkedin\.com\/jobs\/(?:view\/(?:[^/?]*-)?(\d+)|.*[?&]currentJobId=(\d+))/);
  if (!m) return {};
  const id = m[1] || m[2];
  try {
    const res = await fetch(`https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/${id}`, { credentials: "omit" });
    if (!res.ok) return { url: `https://www.linkedin.com/jobs/view/${id}/` };
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    const text = (sel) => (doc.querySelector(sel)?.textContent || "").replace(/\s+/g, " ").trim();
    const job = {
      url: `https://www.linkedin.com/jobs/view/${id}/`,
      source: "linkedin.com",
      title: text(".top-card-layout__title"),
      company: text(".topcard__org-name-link") || text(".topcard__flavor"),
      location: text(".topcard__flavor--bullet"),
      salary: text(".compensation__salary"),
      description: htmlToText(doc.querySelector(".show-more-less-html__markup")),
    };
    return Object.fromEntries(Object.entries(job).filter(([, v]) => v));
  } catch {
    return { url: `https://www.linkedin.com/jobs/view/${id}/` };
  }
}

async function scrapeTab(tab) {
  try {
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["scrape.js"] });
    return result;
  } catch {
    return null; // chrome:// pages, the Web Store, PDFs, etc.
  }
}

async function init() {
  const prefs = await chrome.storage.local.get({ status: "applied", resume_id: "", server: "", token: "" });
  config = { server: prefs.server, token: prefs.token };
  if (!config.server) return showSetup();
  form.hidden = false;
  setStatus(prefs.status);
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  show(msg, "Reading the job…");
  let job = (await scrapeTab(tab)) || { url: tab.url, title: tab.title };
  // LinkedIn's logged-in pages change often; its public guest data is much more reliable.
  job = { ...job, ...(await fetchLinkedIn(job.url || tab.url)) };
  fillForm(job);
  msg.hidden = true;

  let resumes, existing;
  try {
    [resumes, { job: existing }] = await Promise.all([
      api("/api/resumes"),
      api("/api/jobs/lookup?url=" + encodeURIComponent(job.url || "")),
    ]);
  } catch (err) {
    if (err instanceof AuthError) return showSetup("Your login expired — enter the password again.", "err");
    saveBtn.disabled = true;
    return show(msg, `Can't reach ApplyStack at <b>${config.server}</b>. ` +
      (config.server.includes("127.0.0.1") ? "Start it with <b>./run.sh</b>, then reopen this popup." : "Check your connection."), "err");
  }
  form.resume_id.innerHTML = `<option value="">— none —</option>` + resumes
    .map((r) => `<option value="${r.id}">${r.label.replace(/[<>&"]/g, "")}</option>`).join("");
  form.resume_id.value = resumes.some((r) => String(r.id) === String(prefs.resume_id)) ? prefs.resume_id : "";
  // Still missing the basics? Let the tracker read the posting (works for most career sites).
  if (!existing && (!form.company.value || !form.description.value)) {
    try {
      fillForm(await api("/api/import", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: job.url || tab.url }),
      }), true);
    } catch { /* keep whatever the page gave us */ }
  }
  checkSponsorship();
  if (existing) {
    show(msg, `Already tracked as <b>${existing.status}</b>${existing.date_applied ? ` (applied ${existing.date_applied})` : ""}. Saving will fill in anything missing.`);
    if (existing.resume_id) form.resume_id.value = existing.resume_id;
  }
}

// Tell you before saving if the posting says it won't sponsor a visa (the tracker reads the wording).
async function checkSponsorship() {
  const el = document.getElementById("spons");
  if (!form.description.value) return;
  try {
    const r = await api("/api/sponsorship", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: form.description.value }),
    });
    const label = { no: "⚠ This job says it doesn't sponsor visas", mixed: "This job says different things about visa sponsorship",
                    yes: "✓ This job says it sponsors visas" }[r.status];
    if (!r.flag || !label) return;
    el.className = `msg ${r.status === "mixed" ? "" : r.status}`;
    el.innerHTML = `<b>${label}</b>${r.quote ? `<q>${r.quote.replace(/[<>&"]/g, "")}</q>` : ""}`;
    el.hidden = false;
  } catch { /* older trackers don't have this check */ }
}

document.getElementById("statuses").addEventListener("click", (e) => {
  if (e.target.dataset.status) setStatus(e.target.dataset.status);
});

document.getElementById("open-tracker").addEventListener("click", (e) => {
  e.preventDefault();
  if (config.server) chrome.tabs.create({ url: config.server });
});

document.getElementById("settings").addEventListener("click", (e) => {
  e.preventDefault();
  showSetup();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  saveBtn.disabled = true;
  const data = { ...Object.fromEntries(new FormData(form)), status };
  try {
    const res = await api("/api/jobs/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    await chrome.storage.local.set({ status, resume_id: data.resume_id });
    show(msg, `${res.created ? "Saved" : "Updated"} ✓ — <a href="${config.server}/?job=${res.job.id}" target="_blank">open in ApplyStack</a>`, "ok");
    saveBtn.textContent = "Saved";
    setTimeout(() => window.close(), 1500);
  } catch (err) {
    if (err instanceof AuthError) return showSetup("Your login expired — enter the password again.", "err");
    show(msg, err.message, "err");
    saveBtn.disabled = false;
  }
});

init();
