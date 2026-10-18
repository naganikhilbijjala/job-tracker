"use strict";

const form = document.getElementById("f");
const setup = document.getElementById("setup");
const msg = document.getElementById("msg");
const saveBtn = document.getElementById("save");
let status = "applied";
let config = { server: "", token: "" };

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
  setup.server.value = config.server || "";
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
    // Custom domains need permission; *.vercel.app and localhost are granted at install.
    // (The request must run before any other await, while the click still counts as a user gesture.)
    const preGranted = /^https:\/\/[^/]+\.vercel\.app$|^http:\/\/(127\.0\.0\.1|localhost):8765$/.test(origin);
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
  const job = (await scrapeTab(tab)) || { url: tab.url, title: tab.title };
  for (const [k, v] of Object.entries(job)) if (form[k]) form[k].value = v;
  document.getElementById("desc-summary").textContent =
    job.description ? `Job description (${job.description.length.toLocaleString()} chars captured)` : "Job description (none found — paste or highlight it)";

  let resumes, existing;
  try {
    [resumes, { job: existing }] = await Promise.all([
      api("/api/resumes"),
      api("/api/jobs/lookup?url=" + encodeURIComponent(job.url || "")),
    ]);
  } catch (err) {
    if (err instanceof AuthError) return showSetup("Your login expired — enter the password again.", "err");
    saveBtn.disabled = true;
    return show(msg, `Can't reach the tracker at <b>${config.server}</b>. ` +
      (config.server.includes("127.0.0.1") ? "Start it with <b>./run.sh</b>, then reopen this popup." : "Check your connection."), "err");
  }
  form.resume_id.innerHTML = `<option value="">— none —</option>` + resumes
    .map((r) => `<option value="${r.id}">${r.label.replace(/[<>&"]/g, "")}</option>`).join("");
  form.resume_id.value = resumes.some((r) => String(r.id) === String(prefs.resume_id)) ? prefs.resume_id : "";
  if (existing) {
    show(msg, `Already tracked as <b>${existing.status}</b>${existing.date_applied ? ` (applied ${existing.date_applied})` : ""}. Saving will fill in anything missing.`);
    if (existing.resume_id) form.resume_id.value = existing.resume_id;
  }
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
    show(msg, `${res.created ? "Saved" : "Updated"} ✓ — <a href="${config.server}/?job=${res.job.id}" target="_blank">open in tracker</a>`, "ok");
    saveBtn.textContent = "Saved";
    setTimeout(() => window.close(), 1500);
  } catch (err) {
    if (err instanceof AuthError) return showSetup("Your login expired — enter the password again.", "err");
    show(msg, err.message, "err");
    saveBtn.disabled = false;
  }
});

init();
