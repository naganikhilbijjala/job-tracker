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
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), isError ? 5000 : 2500);
}

async function api(path, { method = "GET", json, file, headers = {} } = {}) {
  const opts = { method, headers: { "X-Today": todayISO(), ...headers } };
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

// ------------------------------------------------------------ login

async function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  closeDrawer();
  try { state.meta = await api("/api/meta"); } catch {}
  const { auth = {} } = state.meta;
  $("#google-login").hidden = !auth.google;
  // Single-user trackers just show the password box; with Google sign-in it's tucked away for the admin.
  const pw = $("#password-login");
  pw.hidden = !auth.password;
  pw.open = !auth.google;
  $("summary", pw).hidden = !auth.google;
  const err = new URLSearchParams(location.search).get("login_error");
  if (err) {
    $("#login-error").textContent = err;
    $("#login-error").hidden = false;
    history.replaceState(null, "", "/");
  }
  if (!pw.hidden && pw.open) $("#login-form").password.focus();
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

function cardHTML(j) {
  const meta = [];
  if (j.location) meta.push(`<span>${esc(j.location)}</span>`);
  if (j.date_applied) meta.push(`<span>Applied ${ago(j.date_applied)}</span>`);
  else meta.push(`<span>Saved ${ago(j.created_at)}</span>`);
  if (followUpDue(j)) meta.push(`<span class="chip warn">Follow up</span>`);
  if (j.resume_label) meta.push(`<span class="chip" title="Resume used">📄 ${esc(j.resume_label)}</span>`);
  return `<div class="card" draggable="true" data-id="${j.id}">
    <div class="title">${esc(j.title || "Untitled role")}</div>
    <div class="company">${esc(j.company)}</div>
    <div class="meta">${meta.join("")}</div>
    <select class="move" data-status-for="${j.id}" aria-label="Move to">${statusOptions(j.status)}</select>
  </div>`;
}

function renderBoard() {
  const jobs = state.jobs.filter(matchesSearch);
  $("#view-board").innerHTML = state.meta.statuses.map((s) => {
    const col = jobs.filter((j) => j.status === s);
    return `<div class="column" data-status="${s}">
      <div class="column-head"><span class="dot" style="background:var(--s-${s})"></span>
        ${STATUS_LABELS[s]} <span class="count">${col.length}</span></div>
      ${col.map(cardHTML).join("") || `<div class="empty">${s === "saved" ? "Jobs you want to apply to" : "Nothing here yet"}</div>`}
    </div>`;
  }).join("");
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
      ${cell("Company", `<b>${esc(j.company)}</b>`, "company")}
      ${cell("Title", esc(j.title), "wrap title")}
      ${cell("Status", `<select data-status-for="${j.id}">${statusOptions(j.status)}</select>`, "status")}
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
    ? "Resumes are stored in your tracker's database. Connect Google Drive to also keep copies in a Drive folder."
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
         <p class="muted">Back up your resumes to a “Job Tracker Resumes” folder in your Drive. Free — the app can only see files it creates.</p>
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
       <div id="ext-code" class="row"><button id="make-code">Get connection code</button></div>
       <h3 style="margin-top:16px">Account</h3>
       <p class="muted small">Signed in as ${esc(user.email)}</p>
       <div class="row"><button id="logout">Log out</button></div>`
    : `<h3>Browser extension</h3>
       <p class="muted small">In the extension's setup, use this address${state.meta.auth.required ? " and your tracker password" : ""}:</p>
       <code class="copy">${esc(origin)}</code>
       ${state.meta.auth.required ? `<div class="row"><button id="logout">Log out</button></div>` : ""}`;
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
    toast("Saved");
  } catch (e) {
    toast(e.message, true);
  }
});

$("#delete-job").addEventListener("click", async () => {
  if (!state.editingId || !confirm("Delete this job? This can't be undone.")) return;
  try {
    await api(`/api/jobs/${state.editingId}`, { method: "DELETE" });
    state.jobs = state.jobs.filter((j) => j.id !== state.editingId);
    closeDrawer();
    render();
    toast("Deleted");
  } catch (e) {
    toast(e.message, true);
  }
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
$("#add-job").addEventListener("click", () => openDrawer());

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer();
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
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
      if (!confirm(`Delete “${r.label}” and its file? (Jobs that used it keep their history.)`)) return;
      await api(`/api/resumes/${r.id}`, { method: "DELETE" });
      [state.resumes, state.jobs] = await Promise.all([api("/api/resumes"), api("/api/jobs")]);
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
      if (!confirm("Disconnect Google Drive? Files already in Drive stay there.")) return;
      await api("/api/drive/disconnect", { method: "POST" });
      state.meta = await api("/api/meta");
    } else if (t.id === "logout") {
      await api("/api/logout", { method: "POST" });
      return showLogin();
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
let aiProviders = {};

async function loadProfile() {
  if (profileLoaded) return;
  try {
    const [profile, aiInfo, mcpInfo] = await Promise.all([api("/api/profile"), api("/api/ai"), api("/api/mcp")]);
    $("#profile-text").value = profile.profile;
    $("#instructions-text").value = profile.instructions;
    aiProviders = aiInfo.providers;
    renderAiPanel(aiInfo.config);
    renderMcpPanel(mcpInfo);
    profileLoaded = true;
  } catch (e) {
    toast(e.message, true);
  }
}

function renderAiPanel(config) {
  const options = Object.entries(aiProviders)
    .map(([k, p]) => `<option value="${k}" ${k === config.provider ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  $("#ai-panel").innerHTML = `
    <h3>AI provider</h3>
    <p class="muted small">Used by the ✨ Assistant in each job. You pay the provider directly; the key is stored in your tracker's database and never sent back to the browser.</p>
    <form id="ai-form">
      <label class="field">Provider<select name="provider">${options}</select></label>
      <label class="field">Model
        <div class="row">
          <select id="model-select" hidden></select>
          <input name="model" value="${esc(config.model)}" placeholder="Model ID from the provider's docs">
          <button type="button" id="load-models" title="Load the models your key can use">↻ Models</button>
        </div>
        <span id="model-status" class="muted small"></span>
      </label>
      <label class="field" id="base-url-field">Base URL<input name="base_url" value="${esc(config.base_url)}" placeholder=""></label>
      <label class="field">API key<input name="api_key" type="password" autocomplete="off"
        placeholder="${config.has_key ? `Saved (${esc(config.key_hint)}) — leave blank to keep` : "Paste your API key"}"></label>
      <div class="row"><button type="submit" class="primary">Save</button><button type="button" id="ai-test">Test</button></div>
    </form>`;
  syncProviderFields(false);
  if (config.has_key) loadModels();
}

async function loadModels() {
  const f = $("#ai-form");
  const select = $("#model-select");
  const status = $("#model-status");
  status.textContent = "Loading models…";
  try {
    const models = await api("/api/ai/models");
    const current = f.model.value;
    const options = models.map((m) => `<option value="${esc(m.id)}">${esc(m.name && m.name !== m.id ? `${m.name} — ${m.id}` : m.id)}</option>`);
    if (current && !models.some((m) => m.id === current)) options.unshift(`<option value="${esc(current)}">${esc(current)}</option>`);
    if (!current) options.unshift(`<option value="">Choose a model…</option>`);
    select.innerHTML = options.join("") + `<option value="__other">Other (type an ID)…</option>`;
    select.value = current;
    select.hidden = false;
    f.model.hidden = true;
    status.textContent = `${models.length} models available with your key`;
  } catch (e) {
    select.hidden = true;
    f.model.hidden = false;
    status.textContent = e.message;
  }
}

function syncProviderFields(providerChanged) {
  const f = $("#ai-form");
  const p = aiProviders[f.provider.value] || {};
  $("#base-url-field").hidden = f.provider.value !== "custom" && !f.base_url.value;
  f.base_url.placeholder = p.base_url || "https://…/v1";
  if (providerChanged) f.model.value = p.model || "";
}

async function saveAi() {
  const data = Object.fromEntries(new FormData($("#ai-form")));
  const { config } = await api("/api/ai", { method: "PUT", json: data });
  renderAiPanel(config);
}

function renderMcpPanel({ url, token }) {
  // Running locally we know the project folder; on Vercel the user fills it in.
  const folder = state.meta.resume_dir ? state.meta.resume_dir.replace(/\/data\/resumes$/, "") : "/path/to/job-tracker";
  const desktop = JSON.stringify({ mcpServers: { "job-tracker": {
    command: "python3", args: [`${folder}/mcp_bridge.py`, url, token] } } }, null, 2);
  $("#mcp-panel").innerHTML = `
    <h3>Use with Claude (MCP)</h3>
    <p class="muted small">Lets Claude read your jobs, saved descriptions, profile and tailored resumes, and save new resumes here. Anyone with this token can read and edit your tracker, so keep it private.</p>
    <details><summary><b>Claude Code</b></summary>
      <code class="copy">claude mcp add --transport http job-tracker ${esc(url)} --header "Authorization: Bearer ${esc(token)}"</code></details>
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
  if (e.target.name === "provider" && e.target.closest("#ai-form")) {
    syncProviderFields(true);
    // The model list belongs to the old provider; save, then load again.
    $("#model-select").hidden = true;
    $("#ai-form").model.hidden = false;
    $("#model-status").textContent = "Save, then click ↻ Models to pick from the list.";
  }
  if (e.target.id === "model-select") {
    const input = $("#ai-form").model;
    const other = e.target.value === "__other";
    input.hidden = !other;
    input.value = other ? "" : e.target.value;
    if (other) input.focus();
  }
});
$("#view-profile").addEventListener("click", async (e) => {
  try {
    if (e.target.id === "load-models") {
      await saveAi();  // uses the key just typed, if any
    } else if (e.target.id === "ai-test") {
      e.target.disabled = true;
      e.target.textContent = "Testing…";
      await saveAi();
      const { reply } = await api("/api/ai/test", { method: "POST" });
      toast(`Connected ✓ — the model replied “${reply}”`);
    } else if (e.target.id === "mcp-rotate") {
      if (!confirm("Make a new token? Claude apps using the old one will stop working until you update them.")) return;
      renderMcpPanel(await api("/api/mcp/rotate", { method: "POST" }));
      toast("New token created");
    } else if (e.target.id === "save-profile") {
      await api("/api/profile", { method: "PUT", json: {
        profile: $("#profile-text").value, instructions: $("#instructions-text").value } });
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
      if (!confirm("Delete this tailored resume?")) return;
      await api(`/api/generated/${t.dataset.deleteTailored}`, { method: "DELETE" });
      loadAssistant();
    } else if (t.id === "clear-chat") {
      e.preventDefault();
      if (!confirm("Clear this job's chat history?")) return;
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
      const answer = prompt("How many jobs can this person save? Leave empty for the default.", t.dataset.current);
      if (answer === null) return;
      const limit = answer.trim() === "" ? null : Number(answer);
      if (limit !== null && !(Number.isInteger(limit) && limit >= 0)) return toast("Enter a whole number", true);
      await api(`/api/admin/users/${t.dataset.limit}`, { method: "PUT", json: { job_limit: limit } });
    } else if (t.dataset.status) {
      if (t.dataset.status === "blocked" && !confirm("Turn off this person's access? They're signed out everywhere right away.")) return;
      await api(`/api/admin/users/${t.dataset.user}`, { method: "PUT", json: { status: t.dataset.status } });
    } else if (t.dataset.deleteUser) {
      if (!confirm(`Delete ${t.dataset.email} and all their jobs, resumes and chats? This can't be undone.`)) return;
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

async function boot() {
  state.meta = await api("/api/meta");
  if (state.meta.auth.required && !state.meta.auth.ok) return showLogin();
  $("#login").hidden = true;
  $("#app").hidden = false;
  const admin = !!state.meta.user?.is_admin;
  $('.tabs [data-view="users"]').hidden = !admin;
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
