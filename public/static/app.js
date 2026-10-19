"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const STATUS_LABELS = {
  saved: "Saved", applied: "Applied", interviewing: "Interviewing",
  offer: "Offer", rejected: "Rejected", withdrawn: "Withdrawn",
};
const ACTIVE = new Set(["applied", "interviewing"]);

const state = {
  jobs: [],
  resumes: [],
  meta: { statuses: Object.keys(STATUS_LABELS), drive: {}, auth: {} },
  view: "board",
  search: "",
  statusFilter: "",
  sort: { key: "updated_at", dir: -1 },
  editingId: null,
};

// ------------------------------------------------------------ helpers

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const todayISO = () => new Date().toLocaleDateString("sv"); // YYYY-MM-DD in local time
const localDay = (iso) => (iso.length === 10 ? iso : new Date(iso).toLocaleDateString("sv"));

function daysSince(iso) {
  if (!iso) return null;
  return Math.round((new Date(todayISO()) - new Date(localDay(iso))) / 86400000);
}

function ago(iso) {
  const d = daysSince(iso);
  if (d === null) return "";
  if (d <= 0) return "today";
  if (d === 1) return "yesterday";
  if (d < 30) return `${d}d ago`;
  return new Date(localDay(iso) + "T00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const fmtDate = (iso) => iso
  ? new Date(iso.length === 10 ? iso + "T00:00" : iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
  : "";

function addDays(iso, n) {
  const d = new Date(iso + "T00:00");
  d.setDate(d.getDate() + n);
  return d.toLocaleDateString("sv");
}

const followUpDue = (j) => ACTIVE.has(j.status) && j.follow_up && j.follow_up <= todayISO();

let toastTimer;
// action: optional { label, run } shown as a button in the toast, e.g. Undo.
function toast(msg, isError = false, action = null, ms = isError ? 5000 : 2500) {
  const t = $("#toast");
  const btn = $("#toast-action");
  $("#toast-text").textContent = msg;
  btn.hidden = !action;
  btn.textContent = action?.label || "";
  btn.onclick = action && (() => { t.className = ""; action.run(); });
  t.className = "show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), ms);
}

// In-page replacement for confirm() and prompt(). Resolves true/false, or the typed text / null with `input`.
function ask({ title, body = "", ok = "OK", danger = false, input = null }) {
  const dlg = $("#dialog");
  const field = $("#dialog-input");
  $("#dialog-title").textContent = title;
  $("#dialog-body").textContent = body;
  $("#dialog-body").hidden = !body;
  field.hidden = !input;
  if (input) Object.assign(field, { type: "text", inputMode: "", placeholder: "", value: "", ...input });
  $("#dialog-ok").textContent = ok;
  $("#dialog-ok").className = danger ? "danger-fill" : "primary";
  dlg.returnValue = "";
  dlg.showModal();
  (input ? field : danger ? $("#dialog-cancel") : $("#dialog-ok")).focus();
  return new Promise((resolve) => dlg.addEventListener("close", () => {
    const yes = dlg.returnValue === "ok";
    resolve(input ? (yes ? field.value : null) : yes);
  }, { once: true }));
}
$("#dialog-cancel").addEventListener("click", () => $("#dialog").close());
$("#dialog").addEventListener("click", (e) => { if (e.target === $("#dialog")) $("#dialog").close(); }); // backdrop

// Theme: "system" follows the device; "light" or "dark" is remembered on this device.
function themeChoice() {
  try { return localStorage.getItem("theme") || "system"; } catch { return "system"; }
}
function setTheme(choice) {
  try { choice === "system" ? localStorage.removeItem("theme") : localStorage.setItem("theme", choice); } catch {}
  if (choice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
  syncThemeButtons();
}
const isDark = () => document.documentElement.dataset.theme === "dark" ||
  (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
function syncThemeButtons() {
  $("#landing-theme").setAttribute("aria-label", isDark() ? "Switch to light mode" : "Switch to dark mode");
  $$("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.themeChoice === themeChoice()));
}
$("#landing-theme").addEventListener("click", () => setTheme(isDark() ? "light" : "dark"));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncThemeButtons);
syncThemeButtons();

// A coloured initial for a company, standing in for its logo. Same name, same colour.
function avatarHTML(name, cls = "") {
  const s = String(name || "?").trim();
  let h = 0;
  for (const c of s.toLowerCase()) h = (h * 31 + c.codePointAt(0)) % 360;
  return `<span class="avatar ${cls}" style="--h:${h}" aria-hidden="true">${esc([...s][0]?.toUpperCase() || "?")}</span>`;
}

async function api(path, { method = "GET", json, file, headers = {}, keepalive = false } = {}) {
  const opts = { method, keepalive, headers: { "X-Today": todayISO(), ...headers } };
  if (json !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(json);
  } else if (file) {
    opts.headers["Content-Type"] = "application/octet-stream";
    opts.headers["X-Filename"] = encodeURIComponent(file.name);
    opts.body = file;
  }
  const res = await fetch(path, opts);
  const data = (res.headers.get("content-type") || "").includes("json") ? await res.json() : null;
  if (res.status === 401 && path !== "/api/login") showLogin();
  if (!res.ok) throw new Error(data?.error || `${res.status} ${res.statusText}`);
  return data;
}

function matchesSearch(j) {
  if (!state.search) return true;
  const hay = [j.company, j.title, j.location, j.notes, j.source, j.resume_label].join(" ").toLowerCase();
  return state.search.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

const statusOptions = (cur) => state.meta.statuses
  .map((s) => `<option value="${s}" ${s === cur ? "selected" : ""}>${STATUS_LABELS[s]}</option>`).join("");

// ------------------------------------------------------------ account menu

function renderAccount() {
  const { user, auth = {} } = state.meta;
  const who = user?.name || user?.email || "";
  $("#account-btn").innerHTML = who
    ? esc([...who][0].toUpperCase())
    : `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>`;
  $("#account-btn").title = who || "Settings";
  const choice = (v, label) => `<button type="button" data-theme-choice="${v}">${label}</button>`;
  $("#account-menu").innerHTML = `
    ${user ? `<div class="menu-head"><b>${esc(user.name || user.email)}</b>${user.name ? `<span>${esc(user.email)}</span>` : ""}</div>` : ""}
    <div class="menu-label">Theme</div>
    <div class="seg" role="group" aria-label="Theme">${choice("system", "System")}${choice("light", "Light")}${choice("dark", "Dark")}</div>
    <hr>
    <button type="button" class="menu-item" role="menuitem" data-goto="profile">Profile &amp; AI settings</button>
    <button type="button" class="menu-item" role="menuitem" data-goto="resumes">Resumes &amp; extension</button>
    ${auth.required ? `<hr><button type="button" class="menu-item" role="menuitem" id="logout">Log out</button>` : ""}`;
  syncThemeButtons();
}

function toggleMenu(open = $("#account-menu").hidden) {
  $("#account-menu").hidden = !open;
  $("#account-btn").setAttribute("aria-expanded", open);
}

$("#account-btn").addEventListener("click", () => toggleMenu());
document.addEventListener("click", (e) => {
  if (!e.target.closest(".account")) toggleMenu(false);
});
$("#account-menu").addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.themeChoice) return setTheme(t.dataset.themeChoice);
  toggleMenu(false);
  if (t.dataset.goto) {
    state.view = t.dataset.goto;
    history.replaceState(null, "", "#" + state.view);
    render();
  } else if (t.id === "logout") {
    try {
      flushDelete();
      await api("/api/logout", { method: "POST" });
      location.replace("/");
    } catch (err) {
      toast(err.message, true);
    }
  }
});

// ------------------------------------------------------------ login

async function showLogin() {
  $("#app").hidden = true;
  closeDrawer();
  try { state.meta = await api("/api/meta"); } catch {}
  const { auth = {} } = state.meta;
  const err = new URLSearchParams(location.search).get("login_error");
  if (err) history.replaceState(null, "", "/");
  // With Google sign-in the public landing page is the front door; the password card is for the admin.
  if (auth.google) {
    $("#login").hidden = true;
    $("#landing").hidden = false;
    $("#admin-login").hidden = !auth.password;
    $("#landing-error").textContent = err || "";
    $("#landing-error").hidden = !err;
    startLanding();
  } else {
    showPasswordCard(err);
  }
}

function showPasswordCard(err) {
  $("#landing").hidden = true;
  $("#login").hidden = false;
  $("#back-to-landing").hidden = !state.meta.auth?.google;
  $("#login-error").textContent = err || "";
  $("#login-error").hidden = !err;
  $("#login-form").password.focus();
}

$("#admin-login").addEventListener("click", (e) => {
  e.preventDefault();
  showPasswordCard();
});

// Landing page motion: sections fade up as they scroll in; numbers count up; the extension "types".
let landingStarted = false;
function startLanding() {
  if (landingStarted) return;
  landingStarted = true;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const play = (el) => {
    el.classList.add("in");
    if (still) return;
    $$(".count", el).forEach((n) => {
      const to = Number(n.dataset.to), t0 = performance.now();
      const tick = (t) => {
        const k = Math.min(1, (t - t0) / 1200);
        n.textContent = Math.round(to * (1 - Math.pow(1 - k, 3)));
        if (k < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    $$(".typed", el).forEach((n, i) => {
      const text = n.dataset.text;
      n.textContent = "";
      setTimeout(() => {
        let k = 0;
        const timer = setInterval(() => {
          n.textContent = text.slice(0, ++k);
          if (k >= text.length) clearInterval(timer);
        }, 35);
      }, 300 + i * 650);
    });
  };
  $$(".typed").forEach((n) => (n.textContent = n.dataset.text));
  if (still || !("IntersectionObserver" in window)) return $$("#landing .reveal").forEach(play);
  const seen = new IntersectionObserver((entries) => entries.forEach((e) => {
    if (e.isIntersecting) { play(e.target); seen.unobserve(e.target); }
  }), { threshold: 0.2 });
  $$("#landing .reveal").forEach((el) => seen.observe(el));
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true;
  $("#login-error").hidden = true;
  try {
    await api("/api/login", { method: "POST", json: { password: e.target.password.value } });
    e.target.reset();
    await boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
    $("#login-error").hidden = false;
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------ data

async function loadAll() {
  [state.jobs, state.resumes] = await Promise.all([api("/api/jobs"), api("/api/resumes")]);
  state.jobs = withoutPending(state.jobs);
  render();
}

function upsertJob(job) {
  const i = state.jobs.findIndex((j) => j.id === job.id);
  if (i >= 0) state.jobs[i] = job; else state.jobs.unshift(job);
}

async function updateJob(id, patch) {
  try {
    upsertJob(await api(`/api/jobs/${id}`, { method: "PUT", json: patch }));
    render();
    if (patch.status) toast(`Moved to ${STATUS_LABELS[patch.status]}`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function uploadResume(file) {
  const r = await api("/api/resumes", { method: "POST", file });
  state.resumes = await api("/api/resumes");
  if (r.drive_error) toast(`Saved, but the Drive copy failed: ${r.drive_error}`, true);
  return r;
}

// ------------------------------------------------------------ rendering

function render() {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${state.view}`));
  document.body.dataset.view = state.view;
  renderStats();
  if (state.view === "board") renderBoard();
  else if (state.view === "table") renderTable();
  else if (state.view === "profile") loadProfile();
  else if (state.view === "users") loadUsers();
  else renderResumes();
}

function renderStats() {
  const jobs = state.jobs;
  const applied = jobs.filter((j) => j.date_applied);
  const thisWeek = applied.filter((j) => daysSince(j.date_applied) < 7).length;
  const responded = applied.filter((j) => ["interviewing", "offer"].includes(j.status)).length;
  const count = (s) => jobs.filter((j) => j.status === s).length;
  const due = jobs.filter(followUpDue).length;
  const stats = [
    ["Tracked", jobs.length],
    ["Applied (7 days)", thisWeek],
    ["Applied total", applied.length],
    ["Interviewing", count("interviewing")],
    ["Offers", count("offer")],
    ["Interview rate", applied.length ? Math.round((100 * responded) / applied.length) + "%" : "—"],
  ];
  $("#stats").innerHTML = (due ? `<div class="stat warn"><b>${due}</b><span>Follow-ups due</span></div>` : "") +
    stats.map(([l, v]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
}

// What the description says about visa sponsorship (worked out on the server from its wording).
const SPONSORSHIP = {
  no: ["bad", "No sponsorship", "The description says it doesn't sponsor visas, or needs citizenship or a clearance"],
  mixed: ["warn", "Sponsorship unclear", "The description says different things about visa sponsorship"],
  yes: ["good", "Sponsors visas", "The description says it sponsors visas"],
};

function sponsorshipChip(j) {
  const s = state.meta.flag_sponsorship && SPONSORSHIP[j.sponsorship];
  return s ? `<span class="chip ${s[0]}" title="${s[2]}">${s[1]}</span>` : "";
}

function cardHTML(j) {
  const meta = [];
  if (sponsorshipChip(j)) meta.push(sponsorshipChip(j));
  if (j.location) meta.push(`<span>${esc(j.location)}</span>`);
  if (j.date_applied) meta.push(`<span>Applied ${ago(j.date_applied)}</span>`);
  else meta.push(`<span>Saved ${ago(j.created_at)}</span>`);
  if (followUpDue(j)) meta.push(`<span class="chip warn">Follow up</span>`);
  if (j.resume_label) meta.push(`<span class="chip" title="Resume used">📄 ${esc(j.resume_label)}</span>`);
  return `<div class="card" draggable="true" data-id="${j.id}">
    <div class="card-top">${avatarHTML(j.company)}<div>
      <div class="title">${esc(j.title || "Untitled role")}</div>
      <div class="company">${esc(j.company)}</div>
    </div></div>
    <div class="meta">${meta.join("")}</div>
    <select class="move" data-status-for="${j.id}" aria-label="Move to">${statusOptions(j.status)}</select>
  </div>`;
}

// Shown above the board until the first job is added.
const WELCOME = `<div class="welcome">
  <div><h2>Welcome to ApplyStack</h2>
    <p>Add the jobs you're applying to and follow each one from saved to offer.</p>
    <button type="button" class="primary" id="welcome-add">+ Add your first job</button></div>
  <ol>
    <li><i>1</i><b>Save a job</b>Paste a posting's link and the details fill in.</li>
    <li><i>2</i><b>Add your profile</b>Your full resume on the <a href="#profile" data-goto="profile">Profile</a> tab.</li>
    <li><i>3</i><b>Tailor a resume</b>Open a job and use ✨ Assistant with your AI key.</li>
  </ol></div>`;

function renderBoard() {
  const jobs = state.jobs.filter(matchesSearch);
  $("#view-board").innerHTML = (state.jobs.length ? "" : WELCOME) + `<div class="columns">` + state.meta.statuses.map((s) => {
    const col = jobs.filter((j) => j.status === s);
    return `<div class="column" data-status="${s}">
      <div class="column-head"><span class="dot" style="background:var(--s-${s})"></span>
        ${STATUS_LABELS[s]} <span class="count">${col.length}</span></div>
      ${col.map(cardHTML).join("") || `<div class="empty">${s === "saved" ? "Jobs you want to apply to" : "Nothing here yet"}</div>`}
    </div>`;
  }).join("") + `</div>`;
}

const COLUMNS = [
  ["company", "Company"], ["title", "Title"], ["status", "Status"], ["date_applied", "Applied"],
  ["follow_up", "Follow up"], ["resume_label", "Resume"], ["location", "Location"],
  ["salary", "Salary"], ["source", "Source"], ["updated_at", "Updated"],
];

function renderTable() {
  const { key, dir } = state.sort;
  const jobs = state.jobs
    .filter(matchesSearch)
    .filter((j) => !state.statusFilter || j.status === state.statusFilter)
    .sort((a, b) => {
      const av = key === "status" ? state.meta.statuses.indexOf(a.status) : (a[key] ?? "");
      const bv = key === "status" ? state.meta.statuses.indexOf(b.status) : (b[key] ?? "");
      return (av > bv ? 1 : av < bv ? -1 : 0) * dir;
    });
  // data-label lets the phone layout show each row as a labelled card.
  const cell = (label, html, cls = "") => html ? `<td data-label="${label}" class="${cls}">${html}</td>` : `<td class="blank"></td>`;
  $("#jobs-table").innerHTML = `
    <thead><tr>${COLUMNS.map(([k, l]) =>
      `<th data-key="${k}" class="${k === key ? "sorted" + (dir > 0 ? " asc" : "") : ""}">${l}</th>`).join("")}</tr></thead>
    <tbody>${jobs.map((j) => `<tr data-id="${j.id}">
      ${cell("Company", `<span>${avatarHTML(j.company, "sm")}<b>${esc(j.company)}</b></span>`, "company")}
      ${cell("Title", esc(j.title) + (sponsorshipChip(j) ? " " + sponsorshipChip(j) : ""), "wrap title")}
      ${cell("Status", `<select class="status-pill" style="--c:var(--s-${j.status})" data-status-for="${j.id}" aria-label="Status">${statusOptions(j.status)}</select>`, "status")}
      ${cell("Applied", fmtDate(j.date_applied))}
      ${cell("Follow up", followUpDue(j) ? `<span class="chip warn">${fmtDate(j.follow_up)}</span>` : fmtDate(j.follow_up))}
      ${cell("Resume", esc(j.resume_label || ""))}
      ${cell("Location", esc(j.location))}
      ${cell("Salary", esc(j.salary))}
      ${cell("Source", j.url ? `<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.source || "link")}</a>` : esc(j.source))}
      ${cell("Updated", ago(j.updated_at), "muted")}
    </tr>`).join("") || `<tr><td colspan="${COLUMNS.length}" class="empty">No jobs match.</td></tr>`}</tbody>`;
}

function fmtSize(n) {
  return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
}

function renderResumes() {
  const { drive, storage, resume_dir, origin } = state.meta;
  const hosted = storage === "database";
  $("#storage-note").innerHTML = hosted || !resume_dir
    ? "Resumes are stored in your ApplyStack database. Connect Google Drive to also keep copies in a Drive folder."
    : `Files live in <code>${esc(resume_dir)}</code>. Anything you copy into that folder shows up here automatically.`;
  $("#resume-list").innerHTML = state.resumes.map((r) => {
    const fileUrl = `/files/resumes/${r.id}/${encodeURIComponent(r.filename)}`;
    const driveBtn = r.drive_link
      ? `<a class="btn" href="${esc(r.drive_link)}" target="_blank" rel="noopener">Drive ↗</a>`
      : drive.connected && !r.missing ? `<button data-drive="${r.id}">To Drive</button>` : "";
    return `<div class="resume" data-id="${r.id}">
      <div class="info">
        <input class="label-input" value="${esc(r.label)}" data-label="${r.id}" aria-label="Resume name">
        <div class="file">${esc(r.filename)} · ${r.missing ? "<b>file missing</b>" : fmtSize(r.size || 0)} ·
          ${fmtDate(r.uploaded_at)} · used for ${r.job_count} job${r.job_count === 1 ? "" : "s"}</div>
      </div>
      <div class="actions">
        ${r.missing ? "" : `<a class="btn" href="${fileUrl}" target="_blank" rel="noopener">View</a>`}
        ${driveBtn}
        <button class="danger" data-delete-resume="${r.id}" title="Delete">✕</button>
      </div>
    </div>`;
  }).join("") || `<div class="empty">No resumes yet.</div>`;

  const pending = state.resumes.filter((r) => !r.drive_id && !r.missing).length;
  const credentialStep = hosted
    ? `<li>Under <i>Credentials → Create credentials → OAuth client ID</i>, pick <b>Web application</b> and add this redirect URI:<br><code>${esc(origin)}/oauth2callback</code></li>
       <li>In Vercel → your project → <i>Settings → Environment Variables</i>, add <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code>, then redeploy.</li>`
    : `<li>Under <i>Credentials → Create credentials → OAuth client ID</i>, pick <b>Desktop app</b>, then download the JSON.</li>
       <li>Save it as <code>data/client_secret.json</code> and reload this page.</li>`;
  $("#drive-panel").innerHTML = drive.connected
    ? `<h3>Google Drive ✓</h3>
       <p class="muted">New resumes are copied to your Drive folder automatically.</p>
       <div class="row">
         ${drive.folder_link ? `<a class="btn" href="${esc(drive.folder_link)}" target="_blank" rel="noopener">Open folder ↗</a>` : ""}
         ${pending ? `<button id="drive-sync" class="primary">Upload ${pending} missing</button>` : ""}
         <button id="drive-disconnect">Disconnect</button>
       </div>`
    : drive.configured
      ? `<h3>Google Drive</h3>
         <p class="muted">Back up your resumes to an “ApplyStack Resumes” folder in your Drive. Free — the app can only see files it creates.</p>
         <div class="row"><a class="btn primary" href="/drive/connect">Connect Google Drive</a></div>`
      : `<h3>Google Drive backup (optional, free)</h3>
         <p class="muted">One-time setup, about 5 minutes:</p>
         <ol class="small">
           <li>Open <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">Google Cloud Console</a> and create a project.</li>
           <li>Enable the <a href="https://console.cloud.google.com/apis/library/drive.googleapis.com" target="_blank" rel="noopener">Google Drive API</a>.</li>
           <li>Under <i>OAuth consent screen</i>, choose External and add yourself as a test user.</li>
           ${credentialStep}
         </ol>`;

  const user = state.meta.user;
  $("#connect-panel").innerHTML = user
    ? `<h3>Browser extension</h3>
       <p class="muted small">In the extension's setup, use this address and a connection code:</p>
       <code class="copy">${esc(origin)}</code>
       <div id="ext-code" class="row"><button id="make-code">Get connection code</button></div>`
    : `<h3>Browser extension</h3>
       <p class="muted small">In the extension's setup, use this address${state.meta.auth.required ? " and your ApplyStack password" : ""}:</p>
       <code class="copy">${esc(origin)}</code>`;
}

// ------------------------------------------------------------ drawer

const form = $("#job-form");

function fillResumeSelect(selected) {
  form.resume_id.innerHTML = `<option value="">— none —</option>` +
    state.resumes.map((r) => `<option value="${r.id}">${esc(r.label)}</option>`).join("");
  form.resume_id.value = selected ?? "";
}

async function openDrawer(id = null, prefill = {}) {
  state.editingId = id;
  form.reset();
  form.status.innerHTML = statusOptions("saved");
  // New jobs are usually logged right after applying, so default to Applied today.
  let job = { status: "applied", date_applied: todayISO(), follow_up: addDays(todayISO(), 7), events: [], ...prefill };
  if (id) {
    try { job = await api(`/api/jobs/${id}`); } catch (e) { return toast(e.message, true); }
  }
  for (const k of ["url", "title", "company", "location", "salary", "status", "source",
                   "date_applied", "follow_up", "notes", "description"]) {
    form[k].value = job[k] ?? "";
  }
  fillResumeSelect(job.resume_id);
  showSponsorship(job.sponsorship, job.sponsorship_quote);
  $("#drawer-title").textContent = id ? (job.company ? `${job.title} · ${job.company}` : "Edit job") : "Add job";
  $("#delete-job").hidden = !id;
  syncOpenLink();
  $("#timeline").innerHTML = (job.events || []).map((e) =>
    `<div>${fmtDate(e.at)} — ${esc(e.kind === "created" ? `Added as ${STATUS_LABELS[e.detail] || e.detail}`
      : e.kind === "resume" ? `Resume set to “${e.detail}”` : e.detail.replace(/\w+/g, (s) => STATUS_LABELS[s] || s))}</div>`).join("");
  $("#drawer-tabs [data-pane=assistant]").disabled = !id;
  $("#drawer-tabs [data-pane=assistant]").title = id ? "" : "Save the job first to use the assistant";
  showPane("details");
  $("#scrim").hidden = false;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  if (!matchMedia("(pointer: coarse)").matches) (id ? form.title : form.url).focus();
}

function showSponsorship(status, quote) {
  const note = $("#sponsorship-note");
  note.hidden = !state.meta.flag_sponsorship;
  if (note.hidden) return;
  const s = SPONSORSHIP[status];
  if (!s) {  // say so, rather than show nothing: most postings don't mention sponsorship at all
    note.className = "spons-note none";
    note.innerHTML = form.description.value.trim()
      ? "<b>Visa sponsorship: not mentioned</b> in this description. Worth asking the recruiter."
      : "<b>Visa sponsorship:</b> paste the job description to check it.";
    return;
  }
  note.className = `spons-note ${s[0]}`;
  note.innerHTML = `<b>${s[0] === "bad" ? "⚠ " : s[0] === "good" ? "✓ " : ""}${s[1]}</b>${quote ? `<q>${esc(quote)}</q>` : ""}`;
}

// Check the description as it's pasted or auto-filled, so you know before saving.
let sponsorshipTimer;
function recheckSponsorship() {
  clearTimeout(sponsorshipTimer);
  if (!state.meta.flag_sponsorship) return;
  sponsorshipTimer = setTimeout(async () => {
    try {
      const r = await api("/api/sponsorship", { method: "POST", json: { description: form.description.value } });
      showSponsorship(r.status, r.quote);
    } catch { /* the note is a nicety; saving still works */ }
  }, 400);
}
form.description.addEventListener("input", recheckSponsorship);

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
  state.editingId = null;
}

function syncOpenLink() {
  const a = $("#open-url");
  a.hidden = !/^https?:\/\//.test(form.url.value);
  a.href = form.url.value;
}

async function autofill() {
  const url = form.url.value.trim();
  if (!url) return toast("Paste the job posting link first", true);
  const btn = $("#autofill");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const data = await api("/api/import", { method: "POST", json: { url } });
    for (const [k, v] of Object.entries(data)) {
      if (form[k] && v && (!form[k].value || k === "url")) form[k].value = v;
    }
    recheckSponsorship();
    toast(data.description ? "Filled in from the posting" : "Got the basics — paste the description manually if it's missing");
    syncOpenLink();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Auto-fill";
  }
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  try {
    const job = state.editingId
      ? await api(`/api/jobs/${state.editingId}`, { method: "PUT", json: data })
      : await api("/api/jobs", { method: "POST", json: data });
    upsertJob(job);
    closeDrawer();
    render();
    if (state.meta.flag_sponsorship && job.sponsorship === "no") {
      toast(`Saved. Heads up: ${job.company || "this job"} says it doesn't sponsor visas.`, true);
    } else {
      toast("Saved");
    }
  } catch (e) {
    toast(e.message, true);
  }
});

// Deleting a job hides it at once and offers Undo; the server delete happens when the toast ends.
let pendingDelete = null; // { job, index, timer }

function deleteJob(id) {
  flushDelete();
  const index = state.jobs.findIndex((j) => j.id === id);
  if (index < 0) return;
  const job = state.jobs[index];
  state.jobs.splice(index, 1);
  pendingDelete = { job, index, timer: setTimeout(flushDelete, 6000) };
  render();
  toast(`Deleted “${job.title || job.company}”`, false, { label: "Undo", run: undoDelete }, 6000);
}

function undoDelete() {
  if (!pendingDelete) return;
  const { job, index, timer } = pendingDelete;
  clearTimeout(timer);
  pendingDelete = null;
  state.jobs.splice(Math.min(index, state.jobs.length), 0, job);
  render();
  toast("Restored");
}

function flushDelete() {
  if (!pendingDelete) return;
  const { job, timer } = pendingDelete;
  clearTimeout(timer);
  pendingDelete = null;
  api(`/api/jobs/${job.id}`, { method: "DELETE", keepalive: true }).catch((e) => {
    upsertJob(job);
    render();
    toast(`Couldn't delete: ${e.message}`, true);
  });
}
// Lists fetched while a delete is waiting shouldn't bring the job back.
const withoutPending = (jobs) => jobs.filter((j) => j.id !== pendingDelete?.job.id);
addEventListener("pagehide", flushDelete);

$("#delete-job").addEventListener("click", () => {
  const id = state.editingId;
  if (!id) return;
  closeDrawer();
  deleteJob(id);
});

$("#drawer-resume-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    const r = await uploadResume(file);
    fillResumeSelect(r.id);
    toast(`Uploaded ${r.label}`);
  } catch (e) {
    toast(e.message, true);
  }
});

$("#autofill").addEventListener("click", autofill);
form.url.addEventListener("input", syncOpenLink);
form.url.addEventListener("paste", () => setTimeout(() => {
  if (!form.title.value && /^https?:\/\//.test(form.url.value)) autofill();
}));
form.status.addEventListener("change", () => {
  const status = form.status.value;
  if (["applied", "interviewing", "offer"].includes(status) && !form.date_applied.value) {
    form.date_applied.value = todayISO();
  }
  if (status === "applied" && !form.follow_up.value) form.follow_up.value = addDays(form.date_applied.value, 7);
  if (status === "saved" && !state.editingId) form.date_applied.value = form.follow_up.value = "";
});
$$("[data-close]").forEach((b) => b.addEventListener("click", closeDrawer));
$("#scrim").addEventListener("click", closeDrawer);

// ------------------------------------------------------------ events

$$(".tabs button").forEach((b) => b.addEventListener("click", () => {
  state.view = b.dataset.view;
  history.replaceState(null, "", "#" + state.view);
  render();
}));

$("#search").addEventListener("input", (e) => { state.search = e.target.value.trim(); render(); });

// The logo goes home: the board. Ctrl/⌘/middle-click still opens a new tab, like any link.
$(".brand").addEventListener("click", (e) => {
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault();
  closeDrawer();
  state.view = "board";
  history.replaceState(null, "", "#board");
  render();
  scrollTo({ top: 0, behavior: "smooth" });
});
$("#add-job").addEventListener("click", () => openDrawer());

document.addEventListener("keydown", (e) => {
  if ($("#dialog").open) return;  // the dialog handles its own keys
  if (e.key === "Escape" && !$("#account-menu").hidden) return toggleMenu(false);
  if (e.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer();
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
  if (!typing && pendingDelete && e.key === "z" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); return undoDelete(); }
  if (typing || e.ctrlKey || e.metaKey || $("#app").hidden) return;
  if (e.key === "n") { e.preventDefault(); openDrawer(); }
  if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
});

// Status dropdowns on cards (phones) and in the list view.
document.addEventListener("change", (e) => {
  const id = e.target.dataset?.statusFor;
  if (id) updateJob(Number(id), { status: e.target.value });
});

// board: click + drag and drop (desktop); phones use the dropdown on each card
const board = $("#view-board");
board.addEventListener("click", (e) => {
  if (e.target.closest("#welcome-add")) return openDrawer();
  const go = e.target.closest("[data-goto]");
  if (go) {
    e.preventDefault();
    state.view = go.dataset.goto;
    history.replaceState(null, "", "#" + state.view);
    return render();
  }
  if (e.target.closest("select")) return;
  const card = e.target.closest(".card");
  if (card) openDrawer(Number(card.dataset.id));
});
board.addEventListener("dragstart", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  e.dataTransfer.setData("text/plain", card.dataset.id);
  e.dataTransfer.effectAllowed = "move";
  card.classList.add("dragging");
});
board.addEventListener("dragend", (e) => e.target.closest(".card")?.classList.remove("dragging"));
board.addEventListener("dragover", (e) => {
  const col = e.target.closest(".column");
  if (!col) return;
  e.preventDefault();
  $$(".column.drop").forEach((c) => c !== col && c.classList.remove("drop"));
  col.classList.add("drop");
});
board.addEventListener("dragleave", (e) => {
  const col = e.target.closest(".column");
  if (col && !col.contains(e.relatedTarget)) col.classList.remove("drop");
});
board.addEventListener("drop", (e) => {
  const col = e.target.closest(".column");
  if (!col) return;
  e.preventDefault();
  col.classList.remove("drop");
  const id = Number(e.dataTransfer.getData("text/plain"));
  const job = state.jobs.find((j) => j.id === id);
  if (job && job.status !== col.dataset.status) updateJob(id, { status: col.dataset.status });
});

// list view
$("#status-filter").innerHTML += Object.entries(STATUS_LABELS)
  .map(([s, l]) => `<option value="${s}">${l}</option>`).join("");
$("#status-filter").addEventListener("change", (e) => { state.statusFilter = e.target.value; render(); });
$("#jobs-table").addEventListener("click", (e) => {
  const th = e.target.closest("th");
  if (th) {
    const key = th.dataset.key;
    state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
    return render();
  }
  if (e.target.closest("select, a")) return;
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDrawer(Number(tr.dataset.id));
});

// resumes
async function uploadFiles(files) {
  for (const file of files) {
    try {
      const r = await uploadResume(file);
      toast(`Uploaded ${r.label}`);
    } catch (e) {
      toast(`${file.name}: ${e.message}`, true);
    }
  }
  render();
}
const dz = $("#dropzone");
$("#resume-files").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drop"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drop"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("drop"); uploadFiles([...e.dataTransfer.files]); });

$("#view-resumes").addEventListener("change", async (e) => {
  const id = e.target.dataset.label;
  if (!id) return;
  try {
    await api(`/api/resumes/${id}`, { method: "PUT", json: { label: e.target.value } });
    [state.resumes, state.jobs] = await Promise.all([api("/api/resumes"), api("/api/jobs")]);
    state.jobs = withoutPending(state.jobs);
    toast("Renamed");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#view-resumes").addEventListener("click", async (e) => {
  const t = e.target;
  try {
    if (t.dataset.deleteResume) {
      const r = state.resumes.find((x) => x.id === Number(t.dataset.deleteResume));
      if (!await ask({ title: `Delete “${r.label}”?`, body: "The file is deleted too. Jobs that used it keep their history.",
                       ok: "Delete resume", danger: true })) return;
      await api(`/api/resumes/${r.id}`, { method: "DELETE" });
      [state.resumes, state.jobs] = await Promise.all([api("/api/resumes"), api("/api/jobs")]);
      state.jobs = withoutPending(state.jobs);
      toast("Deleted");
    } else if (t.dataset.drive) {
      t.disabled = true;
      await api(`/api/resumes/${t.dataset.drive}/drive`, { method: "POST" });
      state.resumes = await api("/api/resumes");
      toast("Uploaded to Drive");
    } else if (t.id === "drive-sync") {
      t.disabled = true;
      t.textContent = "Uploading…";
      const res = await api("/api/drive/sync", { method: "POST" });
      state.resumes = await api("/api/resumes");
      toast(res.errors.length ? res.errors[0] : `Uploaded ${res.uploaded} to Drive`, res.errors.length > 0);
    } else if (t.id === "drive-disconnect") {
      if (!await ask({ title: "Disconnect Google Drive?", body: "Files already in your Drive stay there.", ok: "Disconnect" })) return;
      await api("/api/drive/disconnect", { method: "POST" });
      state.meta = await api("/api/meta");
    } else if (t.id === "make-code") {
      const { code } = await api("/api/extension-code", { method: "POST" });
      $("#ext-code").outerHTML = `<code class="copy">${esc(code)}</code>
        <p class="muted small">Paste it into the extension's password box. It works for a year; keep it private.</p>`;
      return;
    } else {
      return;
    }
    render();
  } catch (err) {
    toast(err.message, true);
    render();
  }
});

// ------------------------------------------------------------ profile & AI settings

let profileLoaded = false;
let profileDirty = false;  // unsaved edits in the profile boxes
let aiProviders = {};

function showProfile({ profile, instructions, previous }) {
  $("#profile-text").value = profile;
  $("#instructions-text").value = instructions;
  profileDirty = false;
  renderProfileHistory(previous);
}

function renderProfileHistory(previous) {
  $("#profile-history").innerHTML = previous
    ? `${previous.replaced_by === "Claude" ? "Updated by Claude" : previous.replaced_by === "restore" ? "Restored" : "Changed"}
       ${ago(previous.replaced_at)} · <a href="#" id="profile-restore">Restore previous version</a>`
    : "";
}

$("#profile-text").addEventListener("input", () => (profileDirty = true));
$("#instructions-text").addEventListener("input", () => (profileDirty = true));

async function loadProfile() {
  // Claude can change the profile over MCP, so read it again on every visit (unless you're mid-edit).
  if (profileLoaded) {
    if (!profileDirty) api("/api/profile").then((p) => !profileDirty && showProfile(p)).catch(() => {});
    return;
  }
  try {
    const [profile, aiInfo, mcpInfo, answers] = await Promise.all([
      api("/api/profile"), api("/api/ai"), api("/api/mcp"), api("/api/answers")]);
    showProfile(profile);
    aiProviders = aiInfo.providers;
    renderAiPanel(aiInfo.config);
    renderMcpPanel(mcpInfo);
    renderAnswers(answers);
    profileLoaded = true;
  } catch (e) {
    toast(e.message, true);
  }
}

let aiConfig = { saved: {} };  // what the server has saved, per provider (keys themselves never come back)

function renderAiPanel(config) {
  aiConfig = config;
  const options = Object.entries(aiProviders)
    .map(([k, p]) => `<option value="${k}" ${k === config.provider ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  $("#ai-panel").innerHTML = `
    <h3>AI provider</h3>
    <p class="muted small">Used by the ✨ Assistant in each job. You pay the provider directly; keys are stored in your ApplyStack database and never sent back to the browser.</p>
    <form id="ai-form">
      <label class="field">Provider<select name="provider">${options}</select></label>
      <label class="field">API key <a id="key-link" class="key-link small" target="_blank" rel="noopener">Get a key ↗</a>
        <input name="api_key" type="password" autocomplete="new-password" spellcheck="false"
          data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other"></label>
      <label class="field" id="base-url-field">Base URL<input name="base_url" inputmode="url"></label>
      <label class="field">Model
        <div class="row">
          <select id="model-select" hidden></select>
          <input name="model" placeholder="Model ID from the provider's docs">
          <button type="button" id="load-models" title="Reload the model list" aria-label="Reload the model list">↻</button>
        </div>
        <span id="model-status" class="muted small"></span>
      </label>
      <div class="row"><button type="submit" class="primary">Save</button><button type="button" id="ai-test">Test</button></div>
    </form>`;
  applyProvider(config.provider);
}

// Fill the form with what's saved for this provider, then load its models if there's a key.
function applyProvider(provider) {
  const f = $("#ai-form");
  const p = aiProviders[provider] || {};
  const saved = aiConfig.saved?.[provider] || {};
  f.api_key.value = "";
  f.api_key.placeholder = saved.has_key
    ? `Saved${saved.key_hint ? ` (${saved.key_hint})` : ""}. Leave blank to keep it`
    : `Paste your ${p.label || ""} API key`;
  f.base_url.value = saved.base_url || "";
  f.base_url.placeholder = p.base_url || "https://…/v1";
  $("#base-url-field").hidden = provider !== "custom" && !f.base_url.value;
  f.model.value = saved.model || p.model || "";
  $("#key-link").href = p.keys_url || "#";
  $("#key-link").hidden = !p.keys_url;
  loadModels();
}

// The key the person typed or pasted. A browser or password manager sometimes fills a saved site
// password in here; that's never an API key, so it must not replace the saved one.
function typedKey() {
  const input = $("#ai-form").api_key;
  for (const sel of [":autofill", ":-webkit-autofill"]) {
    try { if (input.matches(sel)) return ""; } catch {}  // older browsers don't know one of these
  }
  return input.value.trim();
}

function setModelStatus(text, isError = false) {
  $("#model-status").textContent = text;
  $("#model-status").classList.toggle("error", isError);
}

function showModelInput() {
  $("#model-select").hidden = true;
  $("#ai-form").model.hidden = false;
}

let modelsRequest = 0;  // only the latest request may update the list (the provider can change mid-request)
async function loadModels() {
  const f = $("#ai-form");
  const provider = f.provider.value;
  const key = typedKey();
  const n = ++modelsRequest;
  if (!key && !aiConfig.saved?.[provider]?.has_key) {
    showModelInput();
    return setModelStatus("Add your API key and the models it can use show up here.");
  }
  if (provider === "custom" && !f.base_url.value.trim()) {
    showModelInput();
    return setModelStatus("Add the server's base URL to load its models.");
  }
  setModelStatus("Loading models…");
  try {
    const models = await api("/api/ai/models", { method: "POST", json: { provider, api_key: key, base_url: f.base_url.value } });
    if (n !== modelsRequest) return;
    if (!models.length) {
      showModelInput();
      return setModelStatus("The provider didn't list any chat models. Type a model ID instead.");
    }
    const select = $("#model-select");
    const current = f.model.value;
    const options = models.map((m) => `<option value="${esc(m.id)}">${esc(m.name && m.name !== m.id ? `${m.name} — ${m.id}` : m.id)}</option>`);
    if (current && !models.some((m) => m.id === current)) options.unshift(`<option value="${esc(current)}">${esc(current)}</option>`);
    if (!current) options.unshift(`<option value="">Choose a model…</option>`);
    select.innerHTML = options.join("") + `<option value="__other">Other (type an ID)…</option>`;
    select.value = current;
    select.hidden = false;
    f.model.hidden = true;
    setModelStatus(`${models.length} models available with ${key ? "this" : "your saved"} key`);
  } catch (e) {
    if (n !== modelsRequest) return;
    showModelInput();
    setModelStatus(`${e.message} You can still type a model ID.`, true);
  }
}

async function saveAi() {
  const data = { ...Object.fromEntries(new FormData($("#ai-form"))), api_key: typedKey() };
  const { config } = await api("/api/ai", { method: "PUT", json: data });
  renderAiPanel(config);
}

// Answers for application forms, saved from the browser extension ("Remember my answers").
function renderAnswers(answers) {
  const item = (a) => `<div class="answer" data-answer="${a.id}">
      <div class="row"><input class="q" value="${esc(a.question)}" title="${esc(a.question)}" aria-label="Question">
        <button type="button" class="icon small danger" data-delete-answer="${a.id}" title="Delete" aria-label="Delete">✕</button></div>
      <textarea class="a" rows="${Math.min(6, Math.max(1, Math.ceil(a.answer.length / 42)))}" aria-label="Answer">${esc(a.answer)}</textarea>
    </div>`;
  $("#answers-panel").innerHTML = `
    <h3>Saved answers <span class="muted small">${answers.length || ""}</span></h3>
    <p class="muted small">The browser extension fills application forms from your profile and these answers. Questions
      like salary, sponsorship or demographics are only ever filled from here, never guessed. In the extension, use
      <b>Remember my answers</b> after filling a form to add more.</p>
    ${answers.length > 4 ? `<input type="search" id="answer-filter" placeholder="Filter answers" autocomplete="off">` : ""}
    <div id="answer-list">${answers.map(item).join("") || `<div class="empty">No saved answers yet.</div>`}</div>
    <details><summary>Add an answer</summary>
      <form id="answer-add">
        <label class="field">Question<input name="question" placeholder="e.g. Will you require visa sponsorship?" required></label>
        <label class="field">Answer<textarea name="answer" rows="2" required></textarea></label>
        <button type="submit" class="primary">Add</button>
      </form></details>`;
}

async function reloadAnswers() {
  renderAnswers(await api("/api/answers"));
}

$("#view-profile").addEventListener("input", (e) => {
  if (e.target.id !== "answer-filter") return;
  const words = e.target.value.toLowerCase().split(/\s+/).filter(Boolean);
  $$("#answer-list .answer").forEach((el) => {
    const hay = (el.querySelector(".q").value + " " + el.querySelector(".a").value).toLowerCase();
    el.hidden = !words.every((w) => hay.includes(w));
  });
});
$("#view-profile").addEventListener("change", async (e) => {
  const row = e.target.closest("[data-answer]");
  if (!row || !e.target.matches(".q, .a")) return;
  try {
    await api(`/api/answers/${row.dataset.answer}`, { method: "PUT",
      json: { question: row.querySelector(".q").value, answer: row.querySelector(".a").value } });
    toast("Answer saved");
  } catch (err) {
    toast(err.message, true);
  }
});
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "answer-add") return;
  e.preventDefault();
  try {
    const data = Object.fromEntries(new FormData(e.target));
    renderAnswers((await api("/api/answers", { method: "POST", json: { items: [data] } })).answers);
    toast("Answer added");
  } catch (err) {
    toast(err.message, true);
  }
});

function renderMcpPanel({ url, token }) {
  // Running locally we know the project folder; on Vercel the user fills it in.
  const folder = state.meta.resume_dir ? state.meta.resume_dir.replace(/\/data\/resumes$/, "") : "/path/to/job-tracker";
  const desktop = JSON.stringify({ mcpServers: { "applystack": {
    command: "python3", args: [`${folder}/mcp_bridge.py`, url, token] } } }, null, 2);
  $("#mcp-panel").innerHTML = `
    <h3>Use with Claude (MCP)</h3>
    <p class="muted small">Lets Claude read your jobs, saved descriptions, profile and tailored resumes, and save new resumes here. Anyone with this token can read and edit your ApplyStack, so keep it private.</p>
    <details><summary><b>Claude Code</b></summary>
      <code class="copy">claude mcp add --scope user --transport http applystack ${esc(url)} --header "Authorization: Bearer ${esc(token)}"</code></details>
    <details><summary><b>claude.ai</b> (Settings → Connectors → Add custom connector)</summary>
      <code class="copy">${esc(url)}?key=${esc(token)}</code></details>
    <details><summary><b>Claude Desktop</b> (Settings → Developer → Edit config)</summary>
      ${state.meta.resume_dir ? "" : `<p class="muted small">Replace <code>/path/to/job-tracker</code> with where the job-tracker folder is on that computer.</p>`}
      <code class="copy pre">${esc(desktop)}</code></details>
    <div class="row"><button id="mcp-rotate">New token</button></div>`;
}

document.addEventListener("submit", async (e) => {
  if (e.target.id !== "ai-form") return;
  e.preventDefault();
  try { await saveAi(); toast("AI settings saved"); } catch (err) { toast(err.message, true); }
});
document.addEventListener("change", (e) => {
  if (e.target.closest("#ai-form")) {
    if (e.target.name === "provider") applyProvider(e.target.value);
    if (e.target.name === "api_key" || e.target.name === "base_url") loadModels();
  }
  if (e.target.id === "model-select") {
    const input = $("#ai-form").model;
    const other = e.target.value === "__other";
    input.hidden = !other;
    input.value = other ? "" : e.target.value;
    if (other) input.focus();
  }
});
// A pasted key loads the models straight away, without waiting for the field to lose focus.
document.addEventListener("paste", (e) => {
  if (e.target.name === "api_key" && e.target.closest("#ai-form")) setTimeout(loadModels);
});
$("#view-profile").addEventListener("click", async (e) => {
  try {
    if (e.target.id === "load-models") {
      loadModels();
    } else if (e.target.id === "ai-test") {
      e.target.disabled = true;
      e.target.textContent = "Testing…";
      await saveAi();
      const { reply } = await api("/api/ai/test", { method: "POST" });
      toast(`Connected ✓ — the model replied “${reply}”`);
    } else if (e.target.dataset.deleteAnswer) {
      const row = e.target.closest("[data-answer]");
      if (!await ask({ title: "Delete this saved answer?", body: row.querySelector(".q").value, ok: "Delete", danger: true })) return;
      await api(`/api/answers/${e.target.dataset.deleteAnswer}`, { method: "DELETE" });
      await reloadAnswers();
      toast("Deleted");
    } else if (e.target.id === "mcp-rotate") {
      if (!await ask({ title: "Make a new token?", body: "Claude apps using the old token stop working until you update them.",
                       ok: "Make new token", danger: true })) return;
      renderMcpPanel(await api("/api/mcp/rotate", { method: "POST" }));
      toast("New token created");
    } else if (e.target.id === "profile-restore") {
      e.preventDefault();
      if (!await ask({ title: "Restore the previous version?", ok: "Restore",
        body: "Your profile and instructions go back to how they were before the last change. The current version is kept, so you can switch back the same way." })) return;
      showProfile(await api("/api/profile/restore", { method: "POST" }));
      toast("Previous version restored");
    } else if (e.target.id === "save-profile") {
      showProfile(await api("/api/profile", { method: "PUT", json: {
        profile: $("#profile-text").value, instructions: $("#instructions-text").value } }));
      $("#profile-status").textContent = "Saved ✓";
      setTimeout(() => ($("#profile-status").textContent = ""), 2000);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    const t = $("#ai-test");
    if (t) { t.disabled = false; t.textContent = "Test"; }
  }
});

// ------------------------------------------------------------ assistant (per job)

const QUICK_PROMPTS = {
  tailor: "Write my resume tailored to this job, following my instructions. Put the complete resume in the ```resume block.",
  skills: "List 10 skills I can add to this application's skills field. Base them on the job description, but only include skills my profile actually supports. Most relevant first, one per line, nothing else.",
  why: "Draft a 3–4 sentence answer to “Why do you want to work here?” for this job, grounded in my real experience. Ready to paste.",
  cover: "Write a short cover letter (under 250 words) for this job, based only on my real experience.",
  match: "How well do I match this job? Give the top strengths and the real gaps, briefly, and how to address each gap honestly.",
};

function showPane(pane) {
  $$("#drawer-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.pane === pane));
  $("#job-form").hidden = pane !== "details";
  $("#assistant").hidden = pane !== "assistant";
  if (pane === "assistant") loadAssistant();
}

$("#drawer-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b && !b.disabled) showPane(b.dataset.pane);
});

function messageHTML(m, i) {
  if (m.role === "user") return `<div class="msg user">${esc(m.content)}</div>`;
  if (m.pending) return `<div class="msg assistant pending"><span class="dots"></span> Thinking… <span id="elapsed"></span></div>`;
  if (m.error) return `<div class="msg assistant error">${esc(m.error)}</div>`;
  // A ```resume block becomes a preview card with a "print to PDF" action.
  const parts = m.content.split(/```resume[^\n]*\n([\s\S]*?)(?:```|$)/);
  const html = parts.map((part, j) => j % 2
    ? `<div class="resume-card"><div class="resume-preview">${renderResume(part)}</div>
         <div class="row"><button type="button" class="primary" data-save-resume="${i}" data-part="${j}">Open &amp; save as PDF</button>
         <button type="button" data-copy="${i}" data-part="${j}">Copy</button></div></div>`
    : renderMarkdown(part)).join("");
  return `<div class="msg assistant">${html}
    <div class="msg-actions"><button type="button" data-copy="${i}">Copy</button></div></div>`;
}

let chat = [];
let chatJobId = null;

function renderChat() {
  $("#chat-log").innerHTML = chat.map(messageHTML).join("") ||
    `<div class="empty">Ask anything about this job — or use a quick action below.
     The assistant knows your profile and this job's description.</div>`;
  const sc = $("#assistant-scroll");
  sc.scrollTop = sc.scrollHeight;
}

async function loadAssistant() {
  const id = state.editingId;
  if (!id) return;
  try {
    const [aiInfo, history, tailored] = await Promise.all([
      api("/api/ai"), api(`/api/jobs/${id}/chat`), api(`/api/generated?job_id=${id}`)]);
    const c = aiInfo.config;
    $("#ai-note").innerHTML = c.has_key
      ? `Using ${esc(aiInfo.providers[c.provider]?.label || c.provider)} · ${esc(c.model)}${history.length ? ` · <a href="#" id="clear-chat">Clear chat</a>` : ""}`
      : `Set up an AI provider on the <a href="#profile" id="goto-profile">Profile tab</a> to use the assistant.`;
    $("#tailored-list").innerHTML = tailored.length
      ? `<div class="tailored"><b>Tailored resumes</b>${tailored.map((t) =>
          `<div class="row"><a href="/resume/${t.id}" target="_blank" rel="noopener">${esc(t.title)}</a>
           ${t.score != null ? `<span class="score-pill ${t.score >= 75 ? "good" : t.score >= 50 ? "warn" : "bad"}" title="Keyword and format score">${t.score}</span>` : ""}
           <span class="muted small">${t.source === "claude" ? "from Claude · " : ""}${ago(t.created_at)}</span>
           <a class="small" href="/api/generated/${t.id}/pdf" title="Download as a one-page PDF">PDF ↓</a>
           <button type="button" class="icon small danger" data-delete-tailored="${t.id}" title="Delete">✕</button></div>`).join("")}</div>`
      : "";
    if (chatJobId !== id || !chat.some((m) => m.pending)) { chat = history; chatJobId = id; }
    renderChat();
  } catch (e) {
    toast(e.message, true);
  }
}

async function sendChat(text) {
  const id = state.editingId;
  if (!id || !text.trim() || chat.some((m) => m.pending)) return;
  chat.push({ role: "user", content: text }, { role: "assistant", pending: true });
  renderChat();
  const started = Date.now();
  const timer = setInterval(() => { const el = $("#elapsed"); if (el) el.textContent = `${Math.round((Date.now() - started) / 1000)}s`; }, 1000);
  try {
    const res = await api(`/api/jobs/${id}/chat`, { method: "POST", json: { message: text } });
    if (chatJobId === id) chat.splice(-2, 2, res.user, res.assistant);
  } catch (e) {
    if (chatJobId === id) chat.splice(-1, 1, { role: "assistant", error: e.message });
  } finally {
    clearInterval(timer);
    if (chatJobId === id && state.editingId === id) renderChat();
  }
}

function resumePart(i, j) {
  const parts = chat[i].content.split(/```resume[^\n]*\n([\s\S]*?)(?:```|$)/);
  return j === undefined ? chat[i].content : parts[j];
}

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const box = e.target.message;
  const text = box.value;
  box.value = "";
  sendChat(text);
});
$("#chat-form").message.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
$("#chips").addEventListener("click", (e) => {
  const key = e.target.closest("button")?.dataset.prompt;
  if (key) sendChat(QUICK_PROMPTS[key]);
});
$("#assistant").addEventListener("click", async (e) => {
  const t = e.target;
  try {
    if (t.dataset.copy !== undefined) {
      const part = t.dataset.part !== undefined ? Number(t.dataset.part) : undefined;
      await navigator.clipboard.writeText(resumePart(Number(t.dataset.copy), part).trim());
      toast("Copied");
    } else if (t.dataset.saveResume !== undefined) {
      const win = window.open("", "_blank");  // open now, while the click still counts (popup blockers)
      const { url } = await api(`/api/jobs/${state.editingId}/generated`, {
        method: "POST", json: { content: resumePart(Number(t.dataset.saveResume), Number(t.dataset.part)) } });
      if (win) win.location = url; else location.href = url;
      loadAssistant();
    } else if (t.dataset.deleteTailored) {
      if (!await ask({ title: "Delete this tailored resume?", body: "Its saved page and score are removed.", ok: "Delete", danger: true })) return;
      await api(`/api/generated/${t.dataset.deleteTailored}`, { method: "DELETE" });
      loadAssistant();
    } else if (t.id === "clear-chat") {
      e.preventDefault();
      if (!await ask({ title: "Clear this chat?", body: "The conversation for this job is deleted. Tailored resumes you saved stay.",
                       ok: "Clear chat", danger: true })) return;
      await api(`/api/jobs/${state.editingId}/chat`, { method: "DELETE" });
      chat = [];
      loadAssistant();
    } else if (t.id === "goto-profile") {
      e.preventDefault();
      closeDrawer();
      state.view = "profile";
      history.replaceState(null, "", "#profile");
      render();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

// ------------------------------------------------------------ users (admin, multi-user mode)

function setLimitBadge(n) {
  $("#limit-badge").textContent = n;
  $("#limit-badge").hidden = !n;
}

const fmtBytes = (n) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);
const atLimit = (u) => u.limit !== null && u.status === "active" && u.jobs >= u.limit;

async function loadUsers() {
  let users, defaultLimit;
  try {
    ({ users, default_limit: defaultLimit } = await api("/api/admin/users"));
  } catch (e) {
    return toast(e.message, true);
  }
  const full = users.filter(atLimit);
  setLimitBadge(full.length);
  users.sort((a, b) => atLimit(b) - atLimit(a));
  const who = (u) => `<b>${esc(u.name || u.email)}</b>${u.name ? `<div class="muted small">${esc(u.email)}</div>` : ""}`;
  $("#users-main").innerHTML = `
    <h3>People</h3>
    ${full.length ? `<p class="warn-note">${full.length === 1 ? "1 person has" : `${full.length} people have`} reached
      their job limit and can't add more. Raise it with <b>Change limit</b>, or leave it.</p>` : ""}
    <div class="table-wrap"><table class="users-table">
      <thead><tr><th>Name</th><th>Access</th><th>Jobs</th><th>Files</th><th>Last sign-in</th><th></th></tr></thead>
      <tbody>${users.map((u) => `<tr class="${atLimit(u) ? "full" : ""}">
        <td>${who(u)}</td>
        <td data-label="Access">${u.id === 1 ? "Admin (you)" : u.status === "active" ? "Active" : "Turned off"}</td>
        <td data-label="Jobs">${u.jobs}${u.limit === null ? "" : ` / ${u.limit}`}${atLimit(u) ? ' <span class="pill-full">at limit</span>' : ""}</td>
        <td data-label="Files">${u.resume_bytes ? fmtBytes(u.resume_bytes) : "—"}</td>
        <td data-label="Last sign-in">${u.last_login ? ago(u.last_login) : "—"}</td>
        <td class="actions">${u.id === 1 ? "" : `<button data-limit="${u.id}" data-current="${u.limit}">Change limit</button>
          ${u.status === "active"
            ? `<button data-user="${u.id}" data-status="blocked">Turn off</button>`
            : `<button data-user="${u.id}" data-status="active">Turn on</button>
               <button class="danger" data-delete-user="${u.id}" data-email="${esc(u.email)}">Delete data</button>`}`}</td>
      </tr>`).join("")}</tbody></table></div>`;
  const bytes = users.reduce((n, u) => n + u.resume_bytes, 0);
  $("#users-usage").innerHTML = `<h3>Usage</h3>
    <p class="muted small">${users.length} ${users.length === 1 ? "person" : "people"} ·
      ${users.reduce((n, u) => n + u.jobs, 0)} jobs · ${fmtBytes(bytes)} of resume files.
      Turso's free plan includes 5 GB.</p>
    <h3 style="margin-top:14px">How access works</h3>
    <p class="muted small">Anyone can sign in with Google. Each person can save up to ${defaultLimit} jobs; they
      don't see the number until they reach it. <b>Turn off</b> signs someone out everywhere, and their data
      stays until you delete it.</p>`;
}

$("#view-users").addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  try {
    if (t.dataset.limit) {
      const answer = await ask({ title: "Change job limit", body: "How many jobs can this person save? Leave empty for the default.",
        ok: "Save", input: { value: t.dataset.current === "null" ? "" : t.dataset.current, inputMode: "numeric" } });
      if (answer === null) return;
      const limit = answer.trim() === "" ? null : Number(answer);
      if (limit !== null && !(Number.isInteger(limit) && limit >= 0)) return toast("Enter a whole number", true);
      await api(`/api/admin/users/${t.dataset.limit}`, { method: "PUT", json: { job_limit: limit } });
    } else if (t.dataset.status) {
      if (t.dataset.status === "blocked" && !await ask({ title: "Turn off this person's access?",
        body: "They're signed out everywhere right away. Their data stays until you delete it.", ok: "Turn off", danger: true })) return;
      await api(`/api/admin/users/${t.dataset.user}`, { method: "PUT", json: { status: t.dataset.status } });
    } else if (t.dataset.deleteUser) {
      if (!await ask({ title: `Delete ${t.dataset.email}?`, body: "All their jobs, resumes and chats are deleted. This can't be undone.",
                       ok: "Delete everything", danger: true })) return;
      await api(`/api/admin/users/${t.dataset.deleteUser}`, { method: "DELETE" });
    } else {
      return;
    }
    loadUsers();
  } catch (err) {
    toast(err.message, true);
  }
});

// ------------------------------------------------------------ boot

const params = new URLSearchParams(location.search);
const hashView = location.hash.slice(1);
if (["board", "table", "resumes", "profile", "users"].includes(hashView)) state.view = hashView;

// Opened from the phone's Share menu (installed app) — pull the link out of whatever was shared.
function sharedJob() {
  const text = [params.get("share_url"), params.get("share_text"), params.get("share_title")].filter(Boolean).join(" ");
  const url = (text.match(/https?:\/\/\S+/) || [])[0];
  return url ? { url } : null;
}

$("#flag-sponsorship").addEventListener("change", async (e) => {
  try {
    const { on } = await api("/api/settings/sponsorship", { method: "PUT", json: { on: e.target.checked } });
    state.meta.flag_sponsorship = on;
    toast(on ? "Jobs will show what they say about sponsorship" : "Sponsorship tags hidden");
  } catch (err) {
    e.target.checked = !e.target.checked;
    toast(err.message, true);
  }
});

async function boot() {
  state.meta = await api("/api/meta");
  if (state.meta.auth.required && !state.meta.auth.ok) return showLogin();
  $("#flag-sponsorship").checked = !!state.meta.flag_sponsorship;
  $("#login").hidden = true;
  $("#landing").hidden = true;
  $("#app").hidden = false;
  const admin = !!state.meta.user?.is_admin;
  $('.tabs [data-view="users"]').hidden = !admin;
  renderAccount();
  setLimitBadge(state.meta.user?.at_limit || 0);
  if (state.view === "users" && !admin) state.view = "board";
  await loadAll();
  if (params.get("drive") === "connected") toast("Google Drive connected");
  if (params.get("drive_error")) toast(`Google Drive: ${params.get("drive_error")}`, true);
  const shared = sharedJob();
  const openId = Number(params.get("job"));
  if (params.size) history.replaceState(null, "", "/#" + state.view);
  if (shared) {
    await openDrawer(null, shared);
    autofill();
  } else if (openId) {
    await openDrawer(openId);
    if (params.get("pane") === "assistant") {
      showPane("assistant");
      // Sent here by "Revise in chat" on a tailored resume's score panel.
      let ask = null;
      try {
        ask = JSON.parse(sessionStorage.getItem("assistant-ask") || "null");
        sessionStorage.removeItem("assistant-ask");
      } catch {}
      if (ask && ask.job === openId && ask.text) {
        await loadAssistant();
        sendChat(ask.text);
      }
    }
  }
}

boot().catch((e) => toast(`Couldn't load: ${e.message}`, true));
