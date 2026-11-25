"use strict";

// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const headers = { "X-JobScout": "1", ...(opts.headers || {}) };
  let body = opts.body;
  if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
  const r = await fetch(path, { method: opts.method || "GET", headers, body });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 7000 : 3000);
}

async function busy(btn, label, fn) {
  const old = btn.textContent; btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = label;
  try { return await fn(); } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = old; }
}

// Clipboard: the async Clipboard API first; then the older execCommand route, which embedded browsers
// (like the desktop window's WebView2) often still allow during a click. Returns true if copied.
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {}
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:0;left:-9999px;opacity:0";
  (document.querySelector("dialog[open]") || document.body).appendChild(ta);
  ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch {}
  ta.remove();
  return ok;
}

const lines = (v) => (v || "").split("\n").map((x) => x.trim()).filter(Boolean);
const fmtDate = (s) => { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
const ago = (s) => {
  if (!s) return ""; const d = new Date(s); if (isNaN(d)) return "";
  const h = (Date.now() - d) / 36e5; if (h < 1) return "just now"; if (h < 24) return `${Math.round(h)}h ago`; return `${Math.round(h / 24)}d ago`;
};
function scorePill(job) {
  if (job.ai_score != null) { const s = job.ai_score; return `<span class="score-pill ${s >= 70 ? "good" : s >= 45 ? "ok" : "bad"}" title="AI match score">${Math.round(s)}</span>`; }
  if (job.keyword_score != null) return `<span class="score-pill kw" title="Keyword score (not yet scored by the AI Agent)">${Math.round(job.keyword_score)}</span>`;
  return "";
}
function salary(job) {
  if (job.salary_text) return job.salary_text;
  if (job.salary_min || job.salary_max) { const f = (n) => n ? "$" + Math.round(n).toLocaleString() : "?"; return `${f(job.salary_min)}–${f(job.salary_max)}`; }
  return "";
}

// Tiny markdown renderer for previews: headings, bullets, bold/italic, paragraphs.
function md(src) {
  const inline = (s) => esc(s).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\*([^*]+)\*/g, "<i>$1</i>");
  let out = "", inList = false;
  for (const raw of (src || "").split("\n")) {
    const l = raw.trimEnd(); let m;
    const close = () => { if (inList) { out += "</ul>"; inList = false; } };
    if (!l.trim()) { close(); continue; }
    if ((m = l.match(/^(#{1,3})\s+(.*)/))) { close(); out += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; }
    else if ((m = l.match(/^\s*[-*•]\s+(.*)/))) { if (!inList) { out += "<ul>"; inList = true; } out += `<li>${inline(m[1])}</li>`; }
    else { close(); out += `<p>${inline(l)}</p>`; }
  }
  return out + (inList ? "</ul>" : "");
}

// ---------- state ----------
const S = { settings: null, jobs: [], currentJob: null, jobSubtab: "description", resumes: [], currentResume: null };

// ---------- tabs ----------
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + name));
  if (name === "mail") loadMail();
  if (name === "resume") loadResumes();
  if (name === "activity") loadActivity();
  if (name === "prefs" || name === "settings") loadSettings();
  if (name === "jobs") loadJobs();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- settings ----------
async function loadSettings() {
  S.settings = await api("/api/settings");
  S.claudeCode = await api("/api/claude-code/status").catch(() => ({ installed: false, enabled: false }));
  S.localLlm = await api("/api/local-llm/status").catch(() => ({ reachable: false, error: "status check failed" }));
  updateSiteLinks();
  renderPrefs(); renderAppSettings(); renderLocalLlm(); renderSecrets(); renderBanner();
  await loadLocalModel();
  $("#debug-dock").classList.toggle("hidden", S.settings.app.debug_panel === false);
  await loadGmailStatus();
  return S.settings;
}

async function loadGmailStatus() {
  S.gmail = await api("/api/gmail/status");
  $("#gmail-query").placeholder = S.gmail.default_query;
  renderGmailBox(); renderBanner();
  return S.gmail;
}

function renderGmailBox() {
  const g = S.gmail, box = $("#gmail-box");
  $("#gmail-help").open = !g.has_client_secret;
  box.innerHTML = `<div class="gmail-state">
    ${g.connected
      ? `<div class="secret-row"><div class="state set">✓ Connected as <b>${esc(g.account || "?")}</b>${g.last_sync ? ` · last sync ${esc(ago(g.last_sync))}` : ""}</div></div>
         <div class="toolbar"><button id="g-sync">Sync now</button><button id="g-disconnect" class="danger">Disconnect</button></div>`
      : `<div class="small"><b>Step 1.</b> Google OAuth client file: ${g.has_client_secret
            ? '<span class="state set">✓ uploaded</span>'
            : '<span style="color:var(--bad)">needed before you can connect</span>'}</div>
         ${g.has_client_secret ? "" : `<div class="small muted">Google only lets apps read Gmail through an OAuth client you own. Follow the <a href="#" id="g-guide-link">one-time setup guide</a> below (about 5 minutes) to download the file, then choose it here.</div>`}
         <div class="toolbar"><label class="small" for="g-secret">${g.has_client_secret ? "Replace file:" : "Choose file:"}</label><input type="file" id="g-secret" accept=".json,application/json"></div>
         <div class="small"><b>Step 2.</b> Sign in with Google (opens in your web browser)</div>
         <div class="toolbar"><button id="g-connect" class="primary">Connect Gmail</button></div>`}
  </div>`;
  const openGuide = () => { const d = $("#gmail-help"); d.open = true; d.scrollIntoView({ behavior: "smooth", block: "start" }); };
  $("#g-guide-link")?.addEventListener("click", (e) => { e.preventDefault(); openGuide(); });
  // Choosing the file uploads it right away (no separate Upload button).
  $("#g-secret")?.addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { await api("/api/gmail/client-secret", { method: "POST", body: fd }); toast("Client file saved. Now click Connect Gmail."); await loadGmailStatus(); }
    catch (err) { toast(err.message, true); e.target.value = ""; }
  });
  $("#g-connect")?.addEventListener("click", (e) => {
    if (!S.gmail.has_client_secret) { toast("Do step 1 first: get the Google OAuth client file using the setup guide below, then choose it above.", true); return openGuide(); }
    connectGmail(e.target);
  });
  $("#g-sync")?.addEventListener("click", (e) => busy(e.target, "Syncing…", async () => {
    const s = await api("/api/gmail/sync", { method: "POST" }); toast(`${s.new} new, ${s.updated} updated, ${s.triaged} sorted`);
    await loadGmailStatus(); updateMailBadge();
  }));
  $("#g-disconnect")?.addEventListener("click", (e) => {
    if (!confirm("Disconnect Gmail? JobScout will revoke its access. Your email stays in Gmail.")) return;
    busy(e.target, "…", async () => { await api("/api/gmail/disconnect", { method: "POST" }); toast("Gmail disconnected"); await loadGmailStatus(); });
  });
}

function connectGmail(btn) {
  busy(btn, "Waiting for Google sign-in in your browser…", async () => {
    const r = await api("/api/gmail/connect", { method: "POST" });
    toast(`Connected as ${r.account}. Syncing job mail…`); await loadGmailStatus();
    const s = await api("/api/gmail/sync", { method: "POST" }); toast(`Gmail synced: ${s.new} job-related thread(s)`);
    await loadGmailStatus(); updateMailBadge();
  });
}

function fillForm(form, values) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in values)) continue;
    const v = values[el.name];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.tagName === "TEXTAREA" && Array.isArray(v)) el.value = v.join("\n");
    else el.value = v ?? "";
  }
  $$("[data-multi]", form).forEach((box) => {
    const set = new Set(values[box.dataset.multi] || []);
    $$("input[type=checkbox]", box).forEach((cb) => (cb.checked = set.has(cb.value)));
  });
}

function readForm(form, listFields = []) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.closest("[data-multi]")) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = el.value === "" ? 0 : Number(el.value);
    else if (listFields.includes(el.name)) out[el.name] = lines(el.value);
    else out[el.name] = el.value;
  }
  $$("[data-multi]", form).forEach((box) => { out[box.dataset.multi] = $$("input:checked", box).map((cb) => cb.value); });
  return out;
}

const PREF_LISTS = ["target_titles", "title_must_include_any", "title_role_any","title_exclude", "keywords_exclude", "keywords_boost", "locations"];

function renderPrefs() {
  const box = $("#source-toggles");
  box.innerHTML = S.settings.sources.map((s) =>
    `<label class="check"><input type="checkbox" value="${esc(s.id)}"> ${esc(s.label)} ${s.ready ? "" : '<span class="badge gray">needs key</span>'}</label>`).join("");
  fillForm($("#prefs-form"), S.settings.preferences);
}
$("#prefs-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { S.settings.preferences = await api("/api/settings/preferences", { method: "PUT", body: readForm(e.target, PREF_LISTS) }); toast("Preferences saved"); }
  catch (err) { toast(err.message, true); }
});

function renderAppSettings() { fillForm($("#ai-form"), S.settings.app); fillForm($("#app-form"), S.settings.app); }

// ---- settings subtabs (Integrations / AI Settings / Profile) -------------
function selectSettingsSubtab(name) {
  S.settingsSubtab = name;
  $$("#tab-settings .settings-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.stab !== name));
  $$("#settings-subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.stab === name));
}
$("#settings-subtabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-stab]");
  if (b) selectSettingsSubtab(b.dataset.stab);
});

function renderLocalLlm() {
  const el = $("#local-llm-status"); if (!el || !S.localLlm) return;
  const l = S.localLlm;
  el.innerHTML = l.reachable
    ? `<span class="state set">✓ reachable</span> ${esc(l.url)} · loaded: ${l.models.map(esc).join(", ") || "?"}` +
      (l.error ? ` · <span style="color:var(--bad)">${esc(l.error)}</span>` : "")
    : `<span style="color:var(--bad)">✗ ${esc(l.error || "unreachable")}</span> <span class="muted">(checked against the saved URL)</span>`;
}

async function checkLocalLlm() {
  S.localLlm = await api("/api/local-llm/status").catch(() => ({ reachable: false, error: "status check failed" }));
  renderLocalLlm(); renderBanner();
}
$("#local-llm-check").addEventListener("click", (e) => busy(e.target, "Checking…", checkLocalLlm));

// ---- built-in local model wizard (Settings > Built-in local model) ----------

let lmTimer = null;

async function loadLocalModel() {
  S.localModel = await api("/api/local-model/status").catch(() => null);
  renderLocalModel();
}

function lmTier(id) { return (S.localModel?.tiers || []).find((t) => t.id === id); }

function renderLocalModel() {
  const s = S.localModel; if (!s) return;
  const hw = s.hardware;
  const gpu = hw.gpus.length
    ? hw.gpus.map((g) => `${esc(g.name)} (${g.vram_gb} GB)`).join(", ")
    : "no GPU found";
  const back = { cuda: "the NVIDIA GPU (CUDA)", vulkan: "the GPU (Vulkan — works with AMD, Intel and Arc)", cpu: "the CPU (no supported GPU found)" }[hw.backend];
  $("#lm-hw").innerHTML =
    `Your hardware: <b>${hw.ram_gb} GB RAM</b> · ${hw.cores} cores · ${gpu} · ${hw.free_gb} GB free. ` +
    `The model will run on <b title="JobScout downloads a matching runtime automatically: CUDA for NVIDIA, Vulkan for AMD/Intel, plain CPU otherwise">${back}</b>.`;
  $("#lm-tiers").innerHTML = s.tiers.map((t) => {
    const badges = [
      t.id === s.recommended ? '<span class="badge" title="Chosen from your RAM, GPU memory and free disk. Pick another if you know better.">Recommended</span>' : "",
      t.running ? '<span class="badge">running</span>' : t.selected ? '<span class="badge gray">installed</span>' : "",
    ].join(" ");
    const btn = t.running ? "" : `<button data-lm="${t.id}">${t.installed ? "Use this" : `Install · ${t.gb} GB`}</button>`;
    const want = t.min_vram ? `best with ${t.min_ram} GB RAM or ${t.min_vram} GB GPU memory` : `best with ${t.min_ram}+ GB RAM`;
    return `<div class="lm-tier${t.selected ? " sel" : ""}"><div><b>${t.label}</b> ${badges}` +
      `<div class="small muted">${esc(t.blurb)} · ${want}</div></div>${btn}</div>`;
  }).join("");
  const inst = s.install;
  if (inst.active || inst.phase === "error") {
    $("#lm-progress").classList.remove("hidden");
    const names = { starting: "Starting…", runtime: "Downloading the GPU/CPU runtime…", "runtime-extract": "Unpacking the runtime…",
                    cudart: "Downloading NVIDIA CUDA support…", model: "Downloading the model…", ready: "Ready" };
    const pct = inst.total ? Math.floor((inst.done / inst.total) * 100) : 0;
    $("#lm-phase").textContent = inst.phase === "error" ? "Install failed" : `${names[inst.phase] || inst.phase} (tier: ${lmTier(inst.tier)?.label || inst.tier})`;
    $("#lm-bar").value = pct;
    $("#lm-pct").textContent = inst.total ? `${pct}% · ${(inst.done / 2**30).toFixed(1)} of ${(inst.total / 2**30).toFixed(1)} GB` : "";
  } else {
    $("#lm-progress").classList.add("hidden");
  }
  $("#lm-status").innerHTML = inst.phase === "error"
    ? `<span style="color:var(--bad)">✗ ${esc(inst.error || "unknown error")}</span> <span class="muted">(your free disk space is fine — try again or pick a smaller model)</span>`
    : s.server.running
      ? `<span class="state set">✓ Running</span> ${esc(lmTier(s.server.tier)?.label || s.server.tier || "")} at ${esc(s.server.url)} — new jobs score with it automatically.`
      : s.selection && lmTier(s.selection)?.installed
        ? `${esc(lmTier(s.selection).label)} is installed but not running — click "Use this" to start it (takes a few seconds).` : "";
  $("#lm-stop").classList.toggle("hidden", !s.server.running);
}

function lmPollUntil(done) {
  clearInterval(lmTimer);
  let n = 0;
  lmTimer = setInterval(async () => {
    if (++n > 150) { clearInterval(lmTimer); return; }
    await loadLocalModel();
    if (S.localModel && done(S.localModel)) { clearInterval(lmTimer); await loadSettings(); }
  }, 1500);
}

$("#lm-tiers").addEventListener("click", async (e) => {
  const id = e.target.dataset.lm; if (!id) return;
  const s = S.localModel, t = lmTier(id);
  if (id !== s.recommended) {
    const rec = lmTier(s.recommended);
    const msg = t.installed
      ? `Use the ${t.label} model even though ${rec.label} suits your hardware better?`
      : `Install the ${t.label} model (${t.gb} GB) even though ${rec.label} is recommended for your hardware? ${t.label} is ${id === "heavy" ? "slower when GPU memory runs short" : "lighter but not as sharp"}.`;
    if (!confirm(msg)) return;
  }
  try {
    if (t.installed) {
      await api("/api/local-model/use", { method: "POST", body: { tier: id } });
      toast(`Starting the ${t.label} model…`);
      lmPollUntil((x) => x.server.running);
    } else {
      await api("/api/local-model/install", { method: "POST", body: { tier: id } });
      toast(`Downloading the ${t.label} model — it stays in ${t.gb} GB of disk.`);
      lmPollUntil((x) => (!x.install.active && (x.server.running || x.install.phase === "error")));
    }
  } catch (err) { toast(err.message, true); }
});

$("#lm-stop").addEventListener("click", (e) => busy(e.target, "…", async () => {
  await api("/api/local-model/stop", { method: "POST" });
  toast("Model stopped — memory freed. Pick 'Use this' any time to start it again.");
  await loadLocalModel();
}));

for (const f of ["#ai-form", "#app-form"]) $(f).addEventListener("submit", async (e) => {
  e.preventDefault();
  try { S.settings.app = await api("/api/settings/app", { method: "PUT", body: readForm(e.target) }); toast("Settings saved"); await loadSettings(); }
  catch (err) { toast(err.message, true); }
});

function syncCleanupDefault() {
  const hasKey = !!S.settings?.secrets?.find((x) => x.name === "anthropic_api_key")?.is_set;
  const local = ["local", "local_builtin", "local_external"].includes(S.settings?.app?.ai_engine);
  const cb = document.querySelector('#resume-upload input[name="cleanup"]');
  if (cb) cb.checked = ccMode() || hasKey || local;
  $("#cleanup-label").textContent = ccMode() ? "Clean up formatting with Claude Code"
    : local ? "Clean up formatting with the local LLM"
    : "Clean up formatting with Claude (needs an API key)";
}

function renderSecrets() {
  syncCleanupDefault();
  $("#secret-list").innerHTML = S.settings.secrets.map((s) => `
    <div class="secret-row" data-name="${esc(s.name)}">
      <div>${esc(s.label)}</div>
      <div class="inputs">
        <input type="password" autocomplete="off" placeholder="${s.is_set ? "Saved. Paste to replace" : "Paste key"}">
        <button class="save">Save</button>${s.is_set ? '<button class="clear danger">Clear</button>' : ""}
      </div>
      <div class="state ${s.is_set ? "set" : "muted"}">${s.is_set ? "✓ Saved " + esc(s.preview) : "Not set"}</div>
    </div>`).join("");
  $$("#secret-list .secret-row").forEach((row) => { // scoped: the Gmail box also uses .secret-row
    const name = row.dataset.name;
    $(".save", row).addEventListener("click", (e) => busy(e.target, "Saving…", async () => {
      const value = $("input", row).value.trim(); if (!value) return toast("Paste a value first", true);
      await api(`/api/settings/secrets/${name}`, { method: "PUT", body: { value } });
      toast("Saved"); await loadSettings();
    }));
    $(".clear", row)?.addEventListener("click", (e) => busy(e.target, "…", async () => {
      await api(`/api/settings/secrets/${name}`, { method: "PUT", body: { value: null } });
      toast("Cleared"); await loadSettings();
    }));
  });
}

function renderBanner() {
  const b = $("#setup-banner"), s = S.settings; if (!s) return;
  const todo = [];
  // Writing tasks need a working engine: the local LLM, an API key, or the integrated Claude Code panel.
  const hasKey = s.secrets.find((x) => x.name === "anthropic_api_key").is_set;
  const ccReady = S.claudeCode?.enabled && S.claudeCode?.installed;
  const localOk = !!S.localLlm?.reachable && !S.localLlm?.error;
  const localEng = ["local", "local_builtin", "local_external"].includes(s.app.ai_engine);
    if (ccMode() && !ccReady) todo.push(`<a data-go="settings:setup">install Claude Code, or switch "AI Agent runs in" to a local LLM or the API</a> (all AI features)`);
  else if (s.app.ai_engine === "local_external" && !(s.app.local_llm_url && s.app.local_llm_model)) todo.push(`<a data-go="settings:setup">set the external LLM URL and model</a> (all AI features)`);
  else if (localEng && !localOk) todo.push(`<a data-go="settings:setup">${s.app.ai_engine === "local_builtin" ? "start the built-in model" : "check the external LLM URL/model"}</a> (all AI features)`);
  else if (!ccMode() && !localEng && !hasKey && !ccReady) todo.push(`<a data-go="settings:account">add an Anthropic API key, switch to a local LLM, or turn on Claude Code</a> (tailoring, cover letters, emails)`);
  if (!s.secrets.find((x) => x.name === "jsearch_api_key").is_set) todo.push(`<a data-go="settings:account">add a JSearch key</a> (Indeed / ZipRecruiter postings)`);
  if (!S.hasResume) todo.push(`<a data-go="resume">upload your resume</a>`);
  if (S.gmail && !S.gmail.connected) todo.push(`<a data-go="settings:account">connect Gmail</a>`);
  b.classList.toggle("hidden", !todo.length);
  b.innerHTML = todo.length ? `<b>Finish setup:</b> ${todo.join(" · ")}` : "";
  $$("a[data-go]", b).forEach((a) => a.addEventListener("click", () => goToTarget(a.dataset.go)));
}

function goToTarget(go) {
  const [tab, sub] = go.split(":");
  showTab(tab);
  if (tab === "settings" && sub) selectSettingsSubtab(sub);
}

// ---------- job sites (open in your normal browser) ----------
function siteUrl(site) {
  const p = S.settings?.preferences || {};
  const q = encodeURIComponent((p.target_titles || ["software engineer"])[0]);
  const loc = encodeURIComponent(p.remote_only ? "Remote" : (p.locations || [""])[0]);
  const days = p.posted_within_days || 7;
  return {
    indeed: `https://www.indeed.com/jobs?q=${q}&l=${loc}&fromage=${days}`,
    ziprecruiter: `https://www.ziprecruiter.com/jobs-search?search=${q}&location=${loc}&days=${days}`,
    linkedin: `https://www.linkedin.com/jobs/search/?keywords=${q}${p.remote_only ? "&f_WT=2" : "&location=" + loc}`,
  }[site];
}
// Plain links (target=_blank) open in the default browser; point them at searches for your first target title.
function updateSiteLinks() { $$("a[data-site]").forEach((a) => { a.href = siteUrl(a.dataset.site); }); }

// ---------- jobs ----------
async function loadJobs() {
  const params = new URLSearchParams({ status: $("#f-status").value, sort: $("#f-sort").value, q: $("#f-q").value, show_hidden: $("#f-hidden").checked });
  const data = await api("/api/jobs?" + params);
  S.jobs = data.jobs; S.unscored = data.unscored || 0;
  $("#hidden-count").textContent = data.hidden_count ? `(${data.hidden_count})` : "";
  const cap = +(S.settings?.preferences?.ai_score_max_per_run || 15); // Claude Code batches its one session to `cap`
  $("#score-batch").textContent = ccMode()
    ? (S.unscored > cap ? `✨ Score the top ${cap} of ${S.unscored} new jobs with the AI Agent`
      : `✨ Score ${S.unscored} new job${S.unscored === 1 ? "" : "s"} with the AI Agent`)
    : `✨ Score ${S.unscored} unscored job${S.unscored === 1 ? "" : "s"} with the AI Agent`;
  $("#score-batch").classList.toggle("hidden", !S.unscored);
  const c = data.counts;
  const order = ["new", "saved", "applying", "applied", "interview", "offer", "low_match", "rejected", "dismissed"];
  $("#counts").textContent = order.filter((k) => c[k]).map((k) => `${k.replace("_", " ")}: ${c[k]}`).join(" · ") || "No jobs yet. Click “Check for jobs now”.";
  renderJobList();
}
function renderJobList() {
  $("#job-list").innerHTML = S.jobs.map((j) => `
    <li data-id="${j.id}" class="${S.currentJob?.id === j.id ? "active" : ""} ${isHidden(j) ? "is-hidden" : ""}">
      <div class="t">${esc(j.title)}</div>
      <div class="score">${scorePill(j)}</div>
      <div class="meta">${esc(j.company || "Unknown company")} · ${esc(j.publisher || j.source)}${j.is_remote ? " · Remote" : ""}${salary(j) ? " · " + esc(salary(j)) : ""}</div>
      <div class="meta">${j.status !== "new" ? `<span class="badge gray">${esc(j.status)}</span> ` : ""}${isHidden(j) ? '<span class="badge gray">hidden</span> ' : ""}${j.posted_at ? "posted " + esc(fmtDate(j.posted_at)) + " · " : ""}found ${esc(ago(j.found_at))}${j.has_tailored ? " · resume ✓" : ""}${j.has_cover ? " · letter ✓" : ""}</div>
      ${j.status === "dismissed" ? "" : j.is_hidden ? `<button class="row-hide" data-unhide="${j.id}" title="Show this job in the list again">Unhide</button>`
        : `<button class="row-hide" data-hide="${j.id}" title="Hide from the list (Show hidden brings it back)">Hide</button>`}
    </li>`).join("");
  $$("#job-list li").forEach((li) => li.addEventListener("click", () => openJob(+li.dataset.id)));
  $$("#job-list [data-hide], #job-list [data-unhide]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation(); setHidden(+(b.dataset.hide || b.dataset.unhide), !!b.dataset.hide);
  }));
}
["#f-status", "#f-sort", "#f-hidden"].forEach((s) => $(s).addEventListener("change", loadJobs));
const isHidden = (j) => !!j.is_hidden; // hidden by you, dismissed, or a new job the AI scored below your cutoff
// Hide / Unhide: hidden jobs stay in the database (scores, letters, emails) but leave the list until "Show hidden".
async function setHidden(id, hidden) {
  await api(`/api/jobs/${id}`, { method: "PATCH", body: { hidden } });
  if (S.currentJob?.id === id) S.currentJob = await api(`/api/jobs/${id}`);
  await loadJobs(); if (S.currentJob?.id === id) renderJob();
  toast(hidden ? "Hidden. Tick “Show hidden” to see it again." : "Back in your list");
}
let qTimer; $("#f-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(loadJobs, 300); });

async function openJob(id) {
  S.currentJob = await api(`/api/jobs/${id}`);
  renderJobList(); renderJob();
}
async function patchJob(values) {
  S.currentJob = await api(`/api/jobs/${S.currentJob.id}`, { method: "PATCH", body: values });
  const i = S.jobs.findIndex((j) => j.id === S.currentJob.id);
  if (i >= 0) Object.assign(S.jobs[i], { status: S.currentJob.status, has_cover: !!S.currentJob.cover_letter, has_tailored: !!S.currentJob.tailored_resume });
  renderJobList();
}

const STATUSES = ["new", "saved", "applying", "applied", "interview", "offer", "rejected", "dismissed", "low_match"];

function renderJob() {
  const j = S.currentJob, el = $("#job-detail");
  const applyUrl = j.apply_url || j.url;
  el.innerHTML = `
    <div class="detail-head">
      <h2>${esc(j.title)}</h2>
      <div class="sub">${esc(j.company || "Unknown company")}${j.location ? " · " + esc(j.location) : ""}</div>
      <div class="badges">
        ${scorePill(j)}
        ${j.is_remote ? '<span class="badge">Remote</span>' : ""}
        ${j.employment_type ? `<span class="badge gray">${esc(j.employment_type)}</span>` : ""}
        ${salary(j) ? `<span class="badge gray">${esc(salary(j))}</span>` : ""}
        <span class="badge gray">via ${esc(j.publisher || j.source)}</span>
        ${j.posted_at ? `<span class="badge gray">posted ${esc(fmtDate(j.posted_at))}</span>` : ""}
      </div>
      <div class="toolbar">
        <label class="small">Status <select id="j-status">${STATUSES.map((s) => `<option ${s === j.status ? "selected" : ""}>${s}</option>`).join("")}</select></label>
        <span class="spacer"></span>
        <button id="j-score">${j.ai_score != null ? "Re-score" : "Score"} with the AI Agent</button>
        ${j.status !== "dismissed" ? `<button id="j-hide">${j.is_hidden ? "Unhide" : "Hide"}</button>` : ""}
        ${j.status !== "dismissed" ? '<button id="j-dismiss" title="Not interested: sets the status to dismissed and hides it">Dismiss</button>' : ""}
        <button id="j-remove" class="danger" title="Delete this job and never fetch it again">Remove</button>
      </div>
    </div>

    ${j.ai_summary ? `
    <div class="ai-box">
      <div><b>AI Agent: ${Math.round(j.ai_score)}/100</b> ${esc(j.ai_summary)}</div>
      <div class="cols">
        <div><b class="small">Strengths</b><ul>${j.ai_strengths.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>
        <div><b class="small">Gaps</b><ul>${j.ai_gaps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>
      </div>
    </div>` : ""}

    <div class="apply-kit">
      <b>Apply kit</b>
      <ol>
        <li>${j.tailored_resume ? "Tailored resume ready ✓" : "Tailor your resume"} <button id="k-tailor">${j.tailored_resume ? "Redo" : "Tailor resume"}</button> ${j.tailored_resume ? '<button id="k-resume-dl">Download</button>' : '<button id="k-resume-dl" title="No tailored version yet: downloads your master resume">Download master</button>'}</li>
        <li>${j.cover_letter ? "Cover letter ready ✓" : "Write a cover letter"} <button id="k-cover">${j.cover_letter ? "Rewrite" : "Write cover letter"}</button> ${j.cover_letter ? '<button id="k-copy-cover">Copy</button> <button id="k-cover-dl">Download</button>' : ""}</li>
        <li>Open the application ${applyUrl ? `<a id="k-open" class="btn-link primary" href="${esc(applyUrl)}" target="_blank" rel="noopener noreferrer">Open in your browser ↗</a>` : '<span class="muted">(no link)</span>'}</li>
        <li>Submit it yourself, then <button id="k-applied">Mark as applied</button></li>
        <li>Reach out or follow up <button id="k-email">✉ Email about this job</button></li>
      </ol>
      <div class="toolbar small"><label>Download as <select id="k-fmt"><option value="pdf">PDF</option><option value="txt">Text (.txt)</option><option value="docx">Word (.docx)</option></select></label>
        <span class="muted">Files go to your Downloads folder, ready to upload on the application site.</span></div>
    </div>

    <div class="subtabs">
      ${["description", "resume", "cover", "emails", "notes"].map((t) => `<button data-sub="${t}" class="${S.jobSubtab === t ? "active" : ""}">${{ description: "Description", resume: "Tailored resume", cover: "Cover letter", emails: "Emails", notes: "Notes" }[t]}</button>`).join("")}
    </div>
    <div id="j-sub"></div>`;

  $("#j-status").addEventListener("change", (e) => patchJob({ status: e.target.value }).catch((err) => toast(err.message, true)));
  $("#j-dismiss")?.addEventListener("click", async () => {
    await patchJob({ status: "dismissed" }); await loadJobs(); renderJob(); toast("Dismissed and hidden. Tick “Show hidden” to see it again.");
  });
  $("#j-hide")?.addEventListener("click", () => setHidden(j.id, !j.is_hidden));
  $("#j-remove").addEventListener("click", async () => {
    if (!confirm(`Remove "${j.title}"${j.company ? ` at ${j.company}` : ""}?\n\nAre you sure? It will be deleted (with its tailored resume, cover letter and notes) and won't be fetched again on Refresh. This can't be undone.`)) return;
    try {
      await api(`/api/jobs/${j.id}`, { method: "DELETE" });
      S.currentJob = null; S.allJobs = null;
      $("#job-detail").innerHTML = `<div class="empty muted">Job removed. It won't come back on Refresh.</div>`;
      await loadJobs(); toast("Removed");
    } catch (err) { toast(err.message, true); }
  });
  // In Claude Code mode (Settings > "AI Agent runs in") the main buttons open a Claude Code session instead of the API.
  $("#j-score").addEventListener("click", (e) => ccMode() ? openClaudeCode({
    title: `Claude Code: score ${j.title}`, request: { kind: "score", job_id: j.id },
    onResult: async () => { await openJob(j.id); await loadJobs(); toast("Score saved from Claude Code"); },
  }) : busy(e.target, "Scoring…", async () => { await api(`/api/jobs/${j.id}/score`, { method: "POST" }); await openJob(j.id); }));
  // One button per task: Settings > "AI Agent runs in" decides who does the work (Claude Code panel, or a direct call).
  $("#k-tailor").addEventListener("click", (e) => ccMode() ? openClaudeCode({
    title: `Claude Code: tailor resume for ${j.title}`, request: { kind: "tailor", job_id: j.id },
    onResult: ccJobResult("Tailored resume", "resume"),
  }) : busy(e.target, "Tailoring… (~30s)", async () => {
    S.currentJob = await api(`/api/jobs/${j.id}/tailor`, { method: "POST" }); S.jobSubtab = "resume"; renderJob(); toast("Tailored resume ready. Review the changes.");
  }));
  $("#k-cover").addEventListener("click", (e) => ccMode() ? openClaudeCode({
    title: `Claude Code: cover letter for ${j.title}`, request: { kind: "cover", job_id: j.id },
    onResult: ccJobResult("Cover letter", "cover"),
  }) : busy(e.target, "Writing… (~30s)", async () => {
    S.currentJob = await api(`/api/jobs/${j.id}/cover-letter`, { method: "POST" }); S.jobSubtab = "cover"; renderJob(); toast("Cover letter ready");
  }));
  // Download format (PDF / TXT / Word) is remembered between jobs.
  try { $("#k-fmt").value = localStorage.getItem("dl.fmt") || "pdf"; } catch {}
  $("#k-fmt").addEventListener("change", (e) => { try { localStorage.setItem("dl.fmt", e.target.value); } catch {} });
  const download = (what) => { location.href = `/api/jobs/${j.id}/${what}.${$("#k-fmt").value}`; toast("Downloading…"); };
  $("#k-resume-dl").addEventListener("click", () => download("resume"));
  $("#k-cover-dl")?.addEventListener("click", () => download("cover-letter"));
  const ccJobResult = (what, sub) => async () => {
    S.currentJob = await api(`/api/jobs/${j.id}`); S.jobSubtab = sub; renderJob(); renderJobList();
    toast(`${what} saved from Claude Code`);
  };
  $("#k-copy-cover")?.addEventListener("click", async () => {
    if (await copyText(S.currentJob.cover_letter)) toast("Cover letter copied");
    else { S.jobSubtab = "cover"; renderJob(); toast("Couldn't copy automatically. Select the letter in the Cover letter tab and press Ctrl+C.", true); }
  });
  $("#k-open")?.addEventListener("click", () => { // the link itself opens the page in the default browser
    if (["new", "saved"].includes(S.currentJob.status)) patchJob({ status: "applying" }).then(renderJob).catch(() => {});
  });
  $("#k-email").addEventListener("click", () => openCompose({ job_id: j.id }));
  $("#k-applied").addEventListener("click", () => patchJob({ status: "applied" }).then(() => { renderJob(); toast("Marked as applied. Good luck!"); }));
  $$(".subtabs button").forEach((b) => b.addEventListener("click", () => { S.jobSubtab = b.dataset.sub; renderJob(); }));
  renderJobSub();
}

function renderJobSub() {
  const j = S.currentJob, box = $("#j-sub");
  if (S.jobSubtab === "description") {
    box.innerHTML = `<div class="desc">${esc(j.description || "No description from this source. Open the posting to read it.")}</div>`;
  } else if (S.jobSubtab === "resume") {
    box.innerHTML = j.tailored_resume ? `
      ${j.resume_changes.length ? `<b class="small">What changed (check these before you send it)</b><ul class="changes small">${j.resume_changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      <textarea id="ed" class="editor">${esc(j.tailored_resume)}</textarea>
      <div class="toolbar"><span class="spacer"></span><button id="ed-save" class="primary">Save edits</button></div>`
      : `<p class="muted">No tailored resume yet. Click <b>Tailor resume</b> in the apply kit. Your master resume is never changed.</p>`;
    $("#ed-save")?.addEventListener("click", (e) => busy(e.target, "Saving…", async () => { await patchJob({ tailored_resume: $("#ed").value }); toast("Saved"); }));
  } else if (S.jobSubtab === "cover") {
    box.innerHTML = j.cover_letter ? `
      <textarea id="ed" class="editor">${esc(j.cover_letter)}</textarea>
      <div class="toolbar"><span class="spacer"></span><button id="ed-save" class="primary">Save edits</button></div>`
      : `<p class="muted">No cover letter yet. Click <b>Write cover letter</b> in the apply kit. It uses the tailored resume if there is one.</p>`;
    $("#ed-save")?.addEventListener("click", (e) => busy(e.target, "Saving…", async () => { await patchJob({ cover_letter: $("#ed").value }); toast("Saved"); }));
  } else if (S.jobSubtab === "emails") {
    box.innerHTML = `<div class="muted small">Loading…</div>`;
    if (!S.gmail?.connected) { box.innerHTML = `<p class="muted">Connect Gmail in Settings to see this job's emails here.</p>`; return; }
    api(`/api/gmail/threads?job_id=${j.id}`).then(({ threads }) => {
      box.innerHTML = threads.length
        ? `<ul class="job-list">${threads.map(threadItem).join("")}</ul>`
        : `<p class="muted">No emails linked to this job yet. Emails are linked automatically by company name, or you can link any conversation from the Mail tab.</p>`;
      $$("li[data-tid]", box).forEach((li) => li.addEventListener("click", () => { showTab("mail"); openThread(li.dataset.tid); }));
    }).catch((e) => (box.innerHTML = `<p class="muted">${esc(e.message)}</p>`));
  } else {
    box.innerHTML = `<textarea id="ed" rows="10" placeholder="Recruiter name, follow-up dates, interview notes…">${esc(j.notes || "")}</textarea>
      <div class="toolbar"><span class="spacer"></span><button id="ed-save" class="primary">Save notes</button></div>`;
    $("#ed-save").addEventListener("click", (e) => busy(e.target, "Saving…", async () => { await patchJob({ notes: $("#ed").value }); toast("Saved"); }));
  }
}

// add job dialog
$("#add-job").addEventListener("click", () => { $("#add-form").reset(); $("#add-dialog").showModal(); });
async function jobAdded(res, how) {
  toast(res.existing ? "Already in your list" : how);
  $("#f-status").value = "active"; await loadJobs(); openJob(res.id);
}
function importWithClaudeCode(text, url) {
  if (text.trim().length < 80) return toast("Paste the full job description text.", true);
  $("#add-dialog").close();
  openClaudeCode({ title: "Claude Code: read a job posting", request: { kind: "import", text, url: url || null },
    onResult: ({ fields }) => jobAdded(fields, "Job added from Claude Code") });
}
$("#add-submit").addEventListener("click", (e) => {
  const f = $("#add-form");
  if (ccMode()) return importWithClaudeCode(f.text.value, f.url.value);
  busy(e.target, "Reading posting…", async () => {
    const res = await api("/api/jobs/import", { method: "POST", body: { url: f.url.value || null, text: f.text.value } });
    $("#add-dialog").close(); await jobAdded(res, "Job added");
  });
});
$("#score-batch").addEventListener("click", (e) => {
  if (ccMode()) {
    openClaudeCode({
      title: "Claude Code: score new jobs", request: { kind: "score" },
      onResult: async ({ fields }) => {
        await loadJobs(); if (S.currentJob) await openJob(S.currentJob.id);
        toast(`The AI Agent scored ${fields.saved} of ${fields.expected} job(s)` + (fields.problems.length ? ` · ${fields.problems.length} couldn't be read` : ""), !!fields.problems.length);
      },
    });
    return;
  }
  busy(e.target, "Scoring…", async () => {
    await api("/api/ai/score-unscored", { method: "POST", body: {} });
    toast("Scoring every unscored job — the list updates as they finish");
    clearInterval(S.scorePoll);
    S.scorePoll = setInterval(async () => {
      await loadJobs();
      if (!S.unscored) { clearInterval(S.scorePoll); toast("All new jobs scored"); }
    }, 10000);
  });
});

// refresh: one job check + one Gmail sync. Nothing runs on a timer.
$("#run-now").addEventListener("click", (e) => busy(e.target, "Refreshing…", async () => {
  const { jobs: r, mail: m } = await api("/api/refresh", { method: "POST" });
  if (r.skipped) return toast("A refresh is already running");
  const scoredNote = ccMode() ? ")" : `, ${r.ai_scored} scored)`;
  let msg = `${r.new} new job${r.new === 1 ? "" : "s"} (${r.fetched} fetched` + scoredNote;
  if (m && !m.error) msg += ` · ${m.new} new email thread${m.new === 1 ? "" : "s"}`;
  if (m?.error) msg += ` · Gmail: ${m.error}`;
  if (r.errors.length) msg += ` · ${r.errors.length} source issue(s), see Activity`;
  await loadJobs();
  if (ccMode() && S.unscored) msg += ` · click “Score ${S.unscored} new with the AI Agent” to rate them`;
  toast(msg, !!m?.error);
  updateRunStatus(); updateMailBadge();
  if (!$("#tab-mail").classList.contains("hidden")) loadMail();
}));

async function updateRunStatus() {
  try {
    const s = await api("/api/monitor/status");
    $("#run-status").textContent = s.running ? "Checking for jobs…" : s.last_run ? `Last refreshed ${ago(s.last_run)}` : "";
    return s;
  } catch { return null; }
}

// ---------- mail ----------
const CAT_LABEL = {
  interview: "Interview", assessment: "Assessment", offer: "Offer", rejection: "Rejection",
  application_received: "Application received", recruiter_outreach: "Recruiter", follow_up: "Follow-up",
  outreach: "My outreach", other: "Other",
};
const nameOf = (addr) => { const m = (addr || "").match(/^\s*"?([^"<]+?)"?\s*</); return m ? m[1] : (addr || "").replace(/[<>]/g, ""); };

function threadItem(t) {
  const needs = t.needs_reply && !t.last_from_me;
  return `<li data-tid="${esc(t.thread_id)}" class="${t.unread ? "unread" : ""} ${S.currentThread?.thread_id === t.thread_id ? "active" : ""}">
    <div class="t subj">${esc(t.subject)}</div>
    <div class="score small muted">${esc(ago(t.last_date))}</div>
    <div class="meta">${esc(nameOf(t.counterpart))}${t.message_count > 1 ? ` (${t.message_count})` : ""}${t.job_title ? ` · ${esc(t.job_company || "")}: ${esc(t.job_title)}` : ""}</div>
    <div class="meta">${needs ? '<span class="chip needs">Needs reply</span> ' : ""}${t.category ? `<span class="chip ${esc(t.category)}">${esc(CAT_LABEL[t.category] || t.category)}</span> ` : ""}${esc(t.ai_summary || t.snippet || "")}</div>
  </li>`;
}

async function updateMailBadge() {
  try {
    if (!S.gmail?.connected) return $("#mail-badge").classList.add("hidden");
    const { counts } = await api("/api/gmail/threads?view=needs_reply");
    const n = counts.needs_reply || 0;
    $("#mail-badge").textContent = n; $("#mail-badge").classList.toggle("hidden", !n);
    $("#mail-badge").title = `${n} conversation(s) need a reply`;
  } catch {}
}

async function loadMail() {
  if (!S.gmail) await loadGmailStatus();
  $("#mail-setup").classList.toggle("hidden", S.gmail.connected);
  $("#mail-main").classList.toggle("hidden", !S.gmail.connected);
  if (!S.gmail.connected) return;
  $("#m-account").textContent = S.gmail.account ? `Signed in as ${S.gmail.account}` : "";
  const params = new URLSearchParams({ view: $("#m-view").value, q: $("#m-q").value });
  const { threads, counts } = await api("/api/gmail/threads?" + params);
  S.threads = threads;
  const un = counts.untriaged || 0;
  // One session sorts the newest 25 (gmail.untriaged_ids); click again for the next batch.
  $("#sort-batch").textContent = un > 25 ? `✨ Sort the newest 25 of ${un} emails with the AI Agent` : `✨ Sort ${un} email${un === 1 ? "" : "s"} with the AI Agent`;
  $("#sort-batch").classList.toggle("hidden", !(ccMode() && un));
  $("#thread-list").innerHTML = threads.map(threadItem).join("") ||
    `<li class="muted">No job-related email ${$("#m-view").value === "all" ? "found yet. Click Refresh, or widen the Gmail search in Settings." : "in this view."}</li>`;
  $$("#thread-list li[data-tid]").forEach((li) => li.addEventListener("click", () => openThread(li.dataset.tid)));
}
$("#m-view").addEventListener("change", loadMail);
let mqTimer; $("#m-q").addEventListener("input", () => { clearTimeout(mqTimer); mqTimer = setTimeout(loadMail, 300); });
$("#m-compose").addEventListener("click", () => openCompose({}));
$("#sort-batch").addEventListener("click", () => openClaudeCode({
  title: "Claude Code: sort new email", request: { kind: "triage" },
  onResult: async ({ fields }) => {
    await loadMail(); updateMailBadge(); if (S.currentThread) openThread(S.currentThread.thread_id);
    toast(`The AI Agent sorted ${fields.saved} of ${fields.expected} conversation(s)` + (fields.problems.length ? ` · ${fields.problems.length} couldn't be read` : ""), !!fields.problems.length);
  },
}));

async function jobOptions() {
  if (!S.allJobs) S.allJobs = (await api("/api/jobs?status=all&limit=1000&sort=newest")).jobs.filter((j) => !["low_match", "dismissed"].includes(j.status));
  return S.allJobs;
}

async function openThread(tid) {
  const box = $("#thread-detail");
  box.innerHTML = `<div class="empty muted">Loading…</div>`;
  try { S.currentThread = await api(`/api/gmail/threads/${encodeURIComponent(tid)}`); }
  catch (e) { box.innerHTML = `<div class="empty muted">${esc(e.message)}</div>`; return; }
  $$("#thread-list li").forEach((li) => { li.classList.toggle("active", li.dataset.tid === tid); if (li.dataset.tid === tid) li.classList.remove("unread"); });
  updateMailBadge();
  renderThread(await jobOptions());
}

function renderThread(jobs) {
  const t = S.currentThread, m = t.meta || {}, box = $("#thread-detail");
  const needs = m.needs_reply && !m.last_from_me;
  const statusSuggestion = m.suggested_status && m.job_id && m.job_status !== m.suggested_status;
  const gmailUrl = `https://mail.google.com/mail/u/?authuser=${encodeURIComponent(S.gmail.account || "")}#all/${encodeURIComponent(t.thread_id)}`;
  box.innerHTML = `
    <div class="detail-head">
      <h2>${esc(t.subject || "(no subject)")}</h2>
      <div class="toolbar">
        <button id="t-reply" class="primary">↩ Reply</button>
        <button id="t-triage">${m.category ? "Re-sort" : "Sort"} with the AI Agent</button>
        ${needs ? '<button id="t-done">Mark handled</button>' : ""}
        ${m.in_inbox ? '<button id="t-archive">Archive</button>' : ""}
        <span class="spacer"></span>
        <a href="${esc(gmailUrl)}" target="_blank" rel="noopener noreferrer" class="small">Open in Gmail ↗</a>
      </div>
    </div>
    <div class="triage">
      <div>${m.category ? `<span class="chip ${esc(m.category)}">${esc(CAT_LABEL[m.category] || m.category)}</span> ` : ""}${needs ? '<span class="chip needs">Needs reply</span> ' : ""}${esc(m.ai_summary || "")}</div>
      <label class="small">Linked job
        <select id="t-job"><option value="">(not linked)</option>${jobs.map((j) => `<option value="${j.id}" ${j.id === m.job_id ? "selected" : ""}>${esc(j.company || "?")}: ${esc(j.title)} [${esc(j.status)}]</option>`).join("")}</select>
      </label>
      ${statusSuggestion ? `<div><button id="t-apply-status">Set job status to “${esc(m.suggested_status)}”</button> <span class="small muted">(currently ${esc(m.job_status)})</span></div>` : ""}
      ${m.job_id ? `<div><a href="#" id="t-open-job" class="small">Open job →</a></div>` : ""}
    </div>
    ${t.messages.map((msg) => `
      <div class="msg ${msg.from_me ? "mine" : ""}">
        <div class="h"><span><b>${esc(msg.from_me ? "You" : nameOf(msg.from))}</b> ${msg.from_me ? "" : `&lt;${esc((msg.from || "").replace(/^.*</, "").replace(/>.*$/, ""))}&gt;`} → ${esc(msg.to || "")}</span><span>${esc(msg.date ? new Date(msg.date).toLocaleString() : "")}</span></div>
        <div class="b">${esc(msg.body || "(no text content)")}</div>
        ${msg.attachments.length ? `<div class="att">📎 ${msg.attachments.map(esc).join(", ")}</div>` : ""}
      </div>`).join("")}`;

  $("#t-reply").addEventListener("click", () => {
    const last = [...t.messages].reverse().find((x) => !x.from_me) || t.messages[t.messages.length - 1];
    const lastMsg = t.messages[t.messages.length - 1];
    const subj = /^re:/i.test(t.subject || "") ? t.subject : `Re: ${t.subject || ""}`;
    openCompose({ thread: t, to: last?.from_me ? last.to : last?.from, subject: subj, job_id: m.job_id, in_reply_to: lastMsg?.message_id, references: lastMsg?.references });
  });
  $("#t-triage").addEventListener("click", (e) => ccMode() ? openClaudeCode({
    title: `Claude Code: sort "${t.subject || "email"}"`, request: { kind: "triage", thread_id: t.thread_id },
    onResult: async () => { await openThread(t.thread_id); loadMail(); updateMailBadge(); toast("Sorted by Claude Code"); },
  }) : busy(e.target, "Sorting…", async () => { await api(`/api/gmail/threads/${encodeURIComponent(t.thread_id)}/triage`, { method: "POST" }); await openThread(t.thread_id); loadMail(); }));
  $("#t-done")?.addEventListener("click", async () => { await api(`/api/gmail/threads/${encodeURIComponent(t.thread_id)}/done`, { method: "POST" }); toast("Marked handled"); await openThread(t.thread_id); loadMail(); });
  $("#t-archive")?.addEventListener("click", (e) => busy(e.target, "…", async () => { await api(`/api/gmail/threads/${encodeURIComponent(t.thread_id)}/archive`, { method: "POST" }); toast("Archived in Gmail"); await openThread(t.thread_id); loadMail(); }));
  $("#t-job").addEventListener("change", async (e) => {
    await api(`/api/gmail/threads/${encodeURIComponent(t.thread_id)}/job`, { method: "PUT", body: { job_id: e.target.value ? +e.target.value : null } });
    toast(e.target.value ? "Linked to job" : "Unlinked"); await openThread(t.thread_id); loadMail();
  });
  $("#t-apply-status")?.addEventListener("click", async () => {
    await api(`/api/jobs/${m.job_id}`, { method: "PATCH", body: { status: m.suggested_status } }); S.allJobs = null;
    toast(`Job marked ${m.suggested_status}`); await openThread(t.thread_id);
  });
  $("#t-open-job")?.addEventListener("click", async (e) => { e.preventDefault(); showTab("jobs"); $("#f-status").value = "all"; await loadJobs(); openJob(m.job_id); });
}

// ---------- compose ----------
async function openCompose({ thread = null, to = "", subject = "", job_id = null, in_reply_to = null, references = null }) {
  if (!S.gmail?.connected) { toast("Connect Gmail in Settings first", true); return showTab("settings"); }
  const f = $("#compose-form"); f.reset();
  S.compose = { thread_id: thread?.thread_id || null, in_reply_to, references };
  $("#compose-title").textContent = thread ? `Reply: ${thread.subject || ""}` : "New email";
  const jobs = await jobOptions();
  f.job_id.innerHTML = `<option value="">(none)</option>` + jobs.map((j) => `<option value="${j.id}">${esc(j.company || "?")}: ${esc(j.title)}</option>`).join("");
  f.to.value = to || ""; f.subject.value = subject || ""; f.job_id.value = job_id || "";
  f.subject.readOnly = !!thread;
  $("#compose-dialog").showModal();
}

function composePayload(draftOnly) {
  const f = $("#compose-form");
  return {
    to: f.to.value.trim(), cc: f.cc.value.trim() || null, subject: f.subject.value.trim(), body: f.body.value,
    job_id: f.job_id.value ? +f.job_id.value : null, attach_resume: f.attach_resume.checked, attach_cover_letter: f.attach_cover_letter.checked,
    thread_id: S.compose.thread_id, in_reply_to: S.compose.in_reply_to, references: S.compose.references, draft_only: draftOnly,
  };
}

$("#compose-ai").addEventListener("click", (e) => ccMode() ? composeWithClaudeCode() : busy(e.target, "Drafting… (~20s)", async () => {
  const f = $("#compose-form");
  const d = await api("/api/gmail/draft-ai", { method: "POST", body: {
    thread_id: S.compose.thread_id, job_id: f.job_id.value ? +f.job_id.value : null, recipient: f.to.value, instructions: f.instructions.value } });
  if (!S.compose.thread_id) f.subject.value = d.subject;
  f.body.value = d.body; toast("Draft ready. Edit it, then Send or save to Gmail drafts.");
}));

async function submitCompose(btn, draftOnly) {
  const p = composePayload(draftOnly);
  if (!p.to) return toast("Add a recipient", true);
  if (!p.subject) return toast("Add a subject", true);
  if (!p.body.trim()) return toast("Write a message (or click Draft reply)", true);
  if (!draftOnly && /\[[^\]]+\]/.test(p.body) && !confirm("The message still has a [placeholder] in it. Send anyway?")) return;
  await busy(btn, draftOnly ? "Saving…" : "Sending…", async () => {
    const r = await api("/api/gmail/send", { method: "POST", body: p });
    $("#compose-dialog").close();
    toast(draftOnly ? "Saved to your Gmail drafts" : `Sent to ${p.to}`);
    if (!$("#tab-mail").classList.contains("hidden")) { await loadMail(); openThread(r.thread_id); }
  });
}
$("#compose-send").addEventListener("click", (e) => submitCompose(e.target, false));
$("#compose-draft").addEventListener("click", (e) => submitCompose(e.target, true));

// ---------- Claude Code panel ----------
// Claude Code mode: Settings > "AI Agent runs in" = Claude Code. Every AI button then opens a session.
function ccMode() { return S.settings?.app?.ai_engine === "claude_code" && S.settings?.app?.claude_code_enabled !== false; }
// A live, interactive Claude Code terminal for one task (runs on the user's own Claude plan).
// Claude saves its answer to result.md in the task folder; we poll and apply it when it changes.
S.cc = { term: null, fit: null, ws: null, session: null, poll: null, onResult: null, applied: {}, dataSub: null };

function ccEnsureTerminal() {
  if (S.cc.term) return;
  const term = new Terminal({
    cursorBlink: true, fontSize: 13, scrollback: 5000, allowProposedApi: false,
    fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, monospace',
    theme: { background: "#11141a", foreground: "#e6e9ef", cursor: "#e6e9ef", selectionBackground: "#3a4a6b" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#cc-term"));
  S.cc.term = term; S.cc.fit = fit;
  new ResizeObserver(() => ccFit()).observe($("#cc-term"));
}

function ccFit() {
  if (!S.cc.fit || !$("#cc-dialog").open) return;
  try { S.cc.fit.fit(); } catch { return; }
  const { rows, cols } = S.cc.term;
  if (S.cc.ws?.readyState === 1) S.cc.ws.send(JSON.stringify({ t: "resize", r: rows, c: cols }));
}

async function openClaudeCode({ title, request, onResult, restart = false }) {
  const st = await api("/api/claude-code/status");
  if (!st.installed) return toast("Claude Code isn't installed on this PC (or its location in Settings is wrong).", true);
  const dlg = $("#cc-dialog");
  $("#cc-title").textContent = title;
  $("#cc-status").textContent = "Starting Claude Code…";
  if (!dlg.open) dlg.showModal();
  ccEnsureTerminal();
  ccFit();
  ccDisconnect();
  S.cc.term.reset();
  S.cc.onResult = onResult; S.cc.request = request; S.cc.title = title;
  let s;
  try {
    s = await api("/api/claude-code/sessions", { method: "POST", body: { ...request, restart, rows: S.cc.term.rows, cols: S.cc.term.cols } });
  } catch (e) { $("#cc-status").textContent = e.message; return toast(e.message, true); }
  S.cc.session = s;
  if (!(s.id in S.cc.applied)) S.cc.applied[s.id] = s.result_mtime; // don't re-apply an older result
  ccConnect(s);
  clearInterval(S.cc.poll); S.cc.poll = setInterval(ccPoll, 2000);
  S.cc.term.focus();
}

function ccConnect(s) {
  const ws = new WebSocket(`ws://${location.host}/api/claude-code/ws/${s.id}?token=${encodeURIComponent(s.token)}`);
  S.cc.ws = ws;
  ws.onopen = () => { $("#cc-status").textContent = s.reused ? "Reconnected to your running session." : "Session started."; ccFit(); };
  ws.onmessage = (e) => S.cc.term.write(e.data);
  ws.onclose = () => { if (S.cc.ws === ws) $("#cc-status").textContent = "Disconnected from the session."; };
  S.cc.dataSub?.dispose();
  S.cc.dataSub = S.cc.term.onData((d) => { if (ws.readyState === 1) ws.send(JSON.stringify({ t: "in", d })); });
}

function ccDisconnect() {
  S.cc.dataSub?.dispose(); S.cc.dataSub = null;
  if (S.cc.ws) { const w = S.cc.ws; S.cc.ws = null; try { w.close(); } catch {} }
}

async function ccPoll() {
  const s = S.cc.session; if (!s) return;
  let st;
  try { st = await api(`/api/claude-code/sessions/${s.id}?token=${encodeURIComponent(s.token)}`); }
  catch { clearInterval(S.cc.poll); return; }
  if (!st.alive) $("#cc-status").textContent = "Session ended. Click Restart to start a new one.";
  if (st.result_mtime && st.result_mtime !== S.cc.applied[s.id]) await ccApply(false);
}

async function ccApply(manual) {
  const s = S.cc.session; if (!s) return;
  try {
    const r = await api(`/api/claude-code/sessions/${s.id}/apply?token=${encodeURIComponent(s.token)}`, { method: "POST" });
    S.cc.applied[s.id] = r.result_mtime;
    await S.cc.onResult?.(r);
    $("#cc-status").textContent = `✓ Result picked up at ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  } catch (e) {
    if (manual) toast(e.message, true);
    else { S.cc.applied[s.id] = (await api(`/api/claude-code/sessions/${s.id}?token=${encodeURIComponent(s.token)}`)).result_mtime; $("#cc-status").textContent = "result.md isn't in the expected format yet: " + e.message; }
  }
}

$("#cc-dialog").addEventListener("cancel", (e) => e.preventDefault()); // Esc belongs to Claude Code (interrupt)
$("#cc-close").addEventListener("click", () => { clearInterval(S.cc.poll); ccDisconnect(); $("#cc-dialog").close(); });
$("#cc-apply").addEventListener("click", () => ccApply(true));
$("#cc-restart").addEventListener("click", () => {
  if (!confirm("Start a fresh Claude Code session for this task? The current conversation ends.")) return;
  openClaudeCode({ title: S.cc.title, request: S.cc.request, onResult: S.cc.onResult, restart: true });
});
$("#cc-stop").addEventListener("click", async () => {
  const s = S.cc.session; if (!s) return;
  await api(`/api/claude-code/sessions/${s.id}/stop?token=${encodeURIComponent(s.token)}`, { method: "POST" }).catch(() => {});
  $("#cc-status").textContent = "Session ended.";
});

function composeWithClaudeCode() {
  const f = $("#compose-form"), isReply = !!S.compose.thread_id;
  openClaudeCode({
    title: isReply ? "Claude Code: reply" : "Claude Code: new email",
    request: { kind: isReply ? "reply" : "outreach", thread_id: S.compose.thread_id, job_id: f.job_id.value ? +f.job_id.value : null,
               recipient: f.to.value, instructions: f.instructions.value },
    onResult: ({ fields }) => {
      if (!isReply && fields.subject) f.subject.value = fields.subject;
      f.body.value = fields.body; toast("Email draft filled in from Claude Code");
    },
  });
}

// ---------- activity ----------
async function loadActivity() {
  const s = await updateRunStatus(); if (!s) return;
  const r = s.last_result;
  $("#last-run").innerHTML = r ? `<b>Last run:</b> ${r.fetched} fetched · ${r.passed_filters} passed filters · ${r.new} new · ${r.ai_scored} AI-scored` +
    (Object.keys(r.by_source || {}).length ? `<br>${Object.entries(r.by_source).map(([k, v]) => `${esc(k)}: ${v.fetched} fetched, ${v.new} new`).join(" · ")}` : "") +
    (r.errors.length ? `<br><span style="color:var(--bad)">${r.errors.map(esc).join("<br>")}</span>` : "") : "No runs yet this session.";
  $("#activity-list").innerHTML = s.activity.map((a) => `<li class="${esc(a.level)}">${esc(new Date(a.at).toLocaleString())}  ${esc(a.message)}</li>`).join("");
}

// ---------- resumes ----------
async function loadResumes(selectId) {
  S.resumes = await api("/api/resumes");
  S.hasResume = S.resumes.length > 0; renderBanner();
  $("#resume-list").innerHTML = S.resumes.map((r) => `<li data-id="${r.id}" class="${S.currentResume?.id === r.id ? "active" : ""}"><span>${esc(r.name)}</span>${r.is_master ? '<span class="badge">Master</span>' : ""}</li>`).join("") || '<li class="muted">None yet</li>';
  $$("#resume-list li[data-id]").forEach((li) => li.addEventListener("click", () => openResume(+li.dataset.id)));
  const id = selectId || S.currentResume?.id || S.resumes[0]?.id;
  if (id) await openResume(id); else showResume(null);
}
async function openResume(id) { showResume(await api(`/api/resumes/${id}`)); $$("#resume-list li").forEach((li) => li.classList.toggle("active", +li.dataset.id === id)); }
function showResume(r) {
  S.currentResume = r;
  $("#resume-name").value = r?.name || ""; $("#resume-content").value = r?.content || "";
  $("#resume-master-badge").classList.toggle("hidden", !r?.is_master);
  ["#resume-save", "#resume-set-master", "#resume-docx", "#resume-delete", "#resume-cleanup"].forEach((s) => ($(s).disabled = !r));
  $("#resume-preview").innerHTML = md(r?.content || "");
}
$("#resume-toggle").addEventListener("click", (e) => {
  const prev = $("#resume-preview"), ta = $("#resume-content"), showing = !prev.classList.contains("hidden");
  if (!showing) prev.innerHTML = md(ta.value);
  prev.classList.toggle("hidden", showing); ta.classList.toggle("hidden", !showing); e.target.textContent = showing ? "Preview" : "Edit";
});
$("#resume-save").addEventListener("click", (e) => busy(e.target, "Saving…", async () => {
  const r = await api(`/api/resumes/${S.currentResume.id}`, { method: "PUT", body: { name: $("#resume-name").value, content: $("#resume-content").value } });
  toast("Resume saved"); await loadResumes(r.id);
}));
$("#resume-set-master").addEventListener("click", (e) => busy(e.target, "…", async () => {
  await api(`/api/resumes/${S.currentResume.id}`, { method: "PUT", body: { make_master: true } }); toast("Master resume set"); await loadResumes(S.currentResume.id);
}));
$("#resume-docx").addEventListener("click", () => (location.href = `/api/resumes/${S.currentResume.id}/docx`));
$("#resume-delete").addEventListener("click", (e) => {
  if (!confirm(`Delete "${S.currentResume.name}"? This can't be undone.`)) return;
  busy(e.target, "…", async () => { await api(`/api/resumes/${S.currentResume.id}`, { method: "DELETE" }); S.currentResume = null; toast("Deleted"); await loadResumes(); });
});
$("#resume-new").addEventListener("click", (e) => busy(e.target, "…", async () => {
  const r = await api("/api/resumes", { method: "POST", body: { name: "New resume", content: "# Your Name\nemail · phone · city · linkedin\n\n## Summary\n\n## Experience\n\n## Skills\n\n## Education\n" } });
  await loadResumes(r.id);
}));
$("#resume-upload").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, fd = new FormData();
  fd.append("file", f.file.files[0]); fd.append("name", f.name.value);
  const cleanup = f.cleanup.checked, viaCC = cleanup && ccMode();
  fd.append("cleanup", cleanup && !viaCC); fd.append("make_master", f.make_master.checked);
  busy($("button[type=submit]", f), cleanup && !viaCC ? "Uploading & cleaning up…" : "Uploading…", async () => {
    const r = await api("/api/resumes/upload", { method: "POST", body: fd }); f.reset(); syncCleanupDefault();
    await loadResumes(r.id);
    if (viaCC) return cleanupResume(r.id);
    if (r.warning) toast(r.warning, true); else toast("Resume uploaded. Review it, then Save.");
  });
});
// The AI Agent fixes the layout of extracted resume text. The result goes into the editor; nothing is saved until Save.
function cleanupResume(rid, btn) {
  const fill = async (markdown) => {
    if (S.currentResume?.id !== rid) await loadResumes(rid);
    $("#resume-content").value = markdown; $("#resume-preview").innerHTML = md(markdown);
    toast("Cleaned-up resume is in the editor. Review it, then click Save.");
  };
  if (ccMode()) return openClaudeCode({ title: "Claude Code: clean up resume formatting", request: { kind: "cleanup", resume_id: rid },
    onResult: ({ fields }) => fill(fields.markdown) });
  return busy(btn, "Cleaning up…", async () => fill((await api(`/api/resumes/${rid}/cleanup`, { method: "POST" })).markdown));
}
$("#resume-cleanup").addEventListener("click", (e) => {
  if (!S.currentResume) return toast("Pick a resume first", true);
  if ($("#resume-content").value !== S.currentResume.content && !confirm("Clean up the last saved version? Unsaved edits in the editor will be replaced.")) return;
  cleanupResume(S.currentResume.id, e.target);
});

// ---------- debug dock (Console + Network) ----------
// Server data comes from /api/debug/* (app requests, outbound calls, server logs; already redacted).
// UI errors/warnings are captured here, shown with source "ui", and forwarded to the server log file.
const D = { open: false, tab: "console", lastC: 0, lastN: 0, console: [], network: [], sel: null, timer: null, uiId: 0 };
const ls = { get(k, d) { try { const v = localStorage.getItem("dbg." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
             set(k, v) { try { localStorage.setItem("dbg." + k, JSON.stringify(v)); } catch {} } };

(function captureUiErrors() {
  let sent = 0;
  const report = (level, parts) => {
    const message = parts.map((p) => (p instanceof Error ? `${p.message}\n${p.stack || ""}` : typeof p === "string" ? p : (() => { try { return JSON.stringify(p); } catch { return String(p); } })())).join(" ");
    D.console.push({ id: `ui${++D.uiId}`, at: new Date().toISOString(), level, source: "ui", message });
    if (D.open && D.tab === "console") renderConsole();
    else updateDockBadges();
    if (sent++ < 50) fetch("/api/debug/client-log", { method: "POST", headers: { "X-JobScout": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ level, message: message.slice(0, 5000) }) }).catch(() => {});
  };
  const origError = console.error.bind(console), origWarn = console.warn.bind(console);
  console.error = (...a) => { origError(...a); report("error", a); };
  console.warn = (...a) => { origWarn(...a); report("warning", a); };
  window.addEventListener("error", (e) => report("error", [e.error || `${e.message} (${e.filename}:${e.lineno})`]));
  window.addEventListener("unhandledrejection", (e) => report("error", ["Unhandled promise rejection:", e.reason]));
})();

const fmtTime = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0"); };
const fmtSize = (n) => (n == null ? "" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
// "jobscout" -> app, "jobscout.activity" -> activity, "uvicorn.error" -> server, "ui" -> ui
const srcLabel = (s) => (!s || s === "jobscout" ? "app" : s.startsWith("jobscout.") ? s.slice(9) : s.startsWith("uvicorn") ? "server" : s);
const LEVEL_RANK = { debug: 0, info: 1, warning: 2, error: 3 };

function setDockOpen(open) {
  D.open = open; ls.set("open", open);
  $("#debug-dock").classList.toggle("collapsed", !open);
  $("#dock-toggle").textContent = (open ? "▾" : "▸") + " Debug";
  if (open) { fetchDebug(); renderDock(); }
  schedulePoll();
}
function setDockTab(tab, fromUser = true) {
  D.tab = tab; ls.set("tab", tab);
  $$(".dock-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.dtab === tab));
  $("#d-console").classList.toggle("hidden", tab !== "console");
  $("#d-network").classList.toggle("hidden", tab !== "network");
  $("#d-level").classList.toggle("hidden", tab !== "console");
  $("#d-kind").classList.toggle("hidden", tab !== "network");
  $("#d-static-wrap").classList.toggle("hidden", tab !== "network");
  if (!D.open && fromUser) setDockOpen(true); else if (D.open) renderDock();
}

function schedulePoll() {
  clearInterval(D.timer);
  // Open: refresh every 1.5s. Closed: every 8s, only to update the error badges.
  D.timer = setInterval(() => (D.open ? fetchDebug() : updateDockBadges(true)), D.open ? 1500 : 8000);
}

async function fetchDebug() {
  try {
    const [c, n] = await Promise.all([
      fetch(`/api/debug/console?after=${D.lastC}`).then((r) => r.json()),
      fetch(`/api/debug/network?after=${D.lastN}`).then((r) => r.json()),
    ]);
    if (c.entries.length) { D.console.push(...c.entries); D.lastC = c.entries[c.entries.length - 1].id; }
    if (n.entries.length) { D.network.push(...n.entries); D.lastN = n.entries[n.entries.length - 1].id; }
    if (D.console.length > 3000) D.console.splice(0, D.console.length - 3000);
    if (D.network.length > 600) D.network.splice(0, D.network.length - 600);
    if (c.entries.length || n.entries.length) renderDock();
  } catch {}
}

async function updateDockBadges(fromServer) {
  let errs = D.console.filter((e) => e.level === "error").length, fails = D.network.filter(isFailed).length;
  if (fromServer) {
    try {
      const s = await fetch("/api/debug/summary").then((r) => r.json());
      errs = s.errors + D.console.filter((e) => e.source === "ui" && e.level === "error").length; fails = s.failed;
    } catch {}
  }
  $("#d-errs").textContent = errs; $("#d-errs").classList.toggle("hidden", !errs);
  $("#d-fails").textContent = fails; $("#d-fails").classList.toggle("hidden", !fails);
  $("#dock-summary").textContent = D.open ? "" : (errs || fails ? `${errs} error${errs === 1 ? "" : "s"} · ${fails} failed request${fails === 1 ? "" : "s"}` : "");
}

const isFailed = (e) => (e.status || 0) >= 400 || !!e.error;

function renderDock() { D.tab === "console" ? renderConsole() : renderNetwork(); updateDockBadges(); }

function renderConsole() {
  const box = $("#d-console"), q = $("#d-filter").value.toLowerCase(), min = $("#d-level").value;
  const rows = D.console
    .filter((e) => min === "all" || LEVEL_RANK[e.level] >= LEVEL_RANK[min])
    .filter((e) => !q || e.message.toLowerCase().includes(q) || (e.source || "").toLowerCase().includes(q))
    .slice(-1000);
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = rows.map((e) => `<div class="row ${esc(e.level)}"><span class="muted">${esc(fmtTime(e.at))}</span>`
    + `<span class="lvl ${esc(e.level)}">${esc(e.level)}</span><span class="muted">${esc(srcLabel(e.source))}</span>`
    + `<span class="cmsg">${esc(e.message)}</span></div>`).join("") || `<div class="row"><span></span><span></span><span></span><span class="muted">Nothing logged yet.</span></div>`;
  if ($("#d-follow").checked || atBottom) box.scrollTop = box.scrollHeight;
}

function renderNetwork() {
  const q = $("#d-filter").value.toLowerCase(), kind = $("#d-kind").value, hideStatic = $("#d-hide-static").checked;
  const rows = D.network
    .filter((e) => !hideStatic || !(e.kind === "app" && (e.url.startsWith("/static/") || e.url === "/")))
    .filter((e) => kind === "all" || (kind === "failed" ? isFailed(e) : e.kind === kind))
    .filter((e) => !q || `${e.method} ${e.url} ${e.status} ${e.label || ""}`.toLowerCase().includes(q))
    .slice(-600);
  const list = $(".net-list"), atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 30;
  $("#net-rows").innerHTML = rows.map((e) => `<tr data-id="${e.id}" class="${isFailed(e) ? "fail" : ""} ${D.sel === e.id ? "sel" : ""}">`
    + `<td class="muted">${esc(fmtTime(e.at))}</td><td>${e.kind === "app" ? "app" : esc(e.label || "outbound")}</td><td>${esc(e.method || "")}</td>`
    + `<td class="url" title="${esc(e.url)}">${esc(e.url)}</td><td>${e.error && !e.status ? "error" : esc(e.status ?? "")}</td>`
    + `<td>${esc(e.ms ?? "")}</td><td>${esc(fmtSize(e.size))}</td></tr>`).join("")
    || `<tr><td colspan="7" class="muted">No requests yet.</td></tr>`;
  $$("#net-rows tr[data-id]").forEach((tr) => tr.addEventListener("click", () => showNetDetail(+tr.dataset.id)));
  if ($("#d-follow").checked || atBottom) list.scrollTop = list.scrollHeight;
}

async function showNetDetail(id) {
  D.sel = id; renderNetwork();
  const box = $("#net-detail"); box.classList.remove("hidden");
  box.innerHTML = `<div class="muted">Loading…</div>`;
  let e;
  try { e = await fetch(`/api/debug/network/${id}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error("gone")))); }
  catch { box.innerHTML = `<div class="muted">This request is no longer in the buffer.</div>`; return; }
  const kv = (o) => o && Object.keys(o).length ? `<div class="kv">${Object.entries(o).map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>` : `<div class="muted">(none)</div>`;
  const pre = (t) => (t ? `<pre>${esc(t)}</pre>` : `<div class="muted">(empty)</div>`);
  box.innerHTML = `<div class="toolbar"><b>${esc(e.method)} ${esc(e.status ?? "")}</b><span class="spacer"></span><button type="button" id="net-close">✕</button></div>
    <div class="kv"><span>URL</span><span>${esc(e.url)}</span><span>Type</span><span>${esc(e.kind === "app" ? "App API" : (e.label || "outbound"))}</span>
      <span>Time</span><span>${esc(new Date(e.at).toLocaleString())}</span><span>Duration</span><span>${esc(e.ms)} ms</span>
      ${e.error ? `<span>Error</span><span style="color:var(--bad)">${esc(e.error)}</span>` : ""}</div>
    <h4>Request headers</h4>${kv(e.request?.headers)}
    <h4>Request body</h4>${pre(e.request?.body)}
    <h4>Response headers</h4>${kv(e.response?.headers)}
    <h4>Response body</h4>${pre(e.response?.body)}
    <p class="small muted">Keys, tokens and passwords are replaced with ***.</p>`;
  $("#net-close").addEventListener("click", () => { D.sel = null; box.classList.add("hidden"); renderNetwork(); });
}

(function initDock() {
  $("#dock-toggle").addEventListener("click", () => setDockOpen(!D.open));
  $$(".dock-tabs button").forEach((b) => b.addEventListener("click", () => setDockTab(b.dataset.dtab)));
  ["#d-filter", "#d-level", "#d-kind", "#d-hide-static"].forEach((s) => $(s).addEventListener("input", renderDock));
  $("#d-clear").addEventListener("click", async () => {
    await fetch("/api/debug/clear", { method: "POST", headers: { "X-JobScout": "1" } }).catch(() => {});
    D.console = []; D.network = []; D.sel = null; $("#net-detail").classList.add("hidden"); renderDock();
  });
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "`" && !$("#cc-dialog").open) { e.preventDefault(); setDockOpen(!D.open); }
  });
  // Drag the top edge to resize.
  const dock = $("#debug-dock");
  dock.style.setProperty("--dock-h", ls.get("height", 300) + "px");
  $("#dock-resize").addEventListener("pointerdown", (e) => {
    const startY = e.clientY, startH = dock.getBoundingClientRect().height;
    const move = (ev) => dock.style.setProperty("--dock-h", Math.max(120, Math.min(window.innerHeight * 0.8, startH + (startY - ev.clientY))) + "px");
    const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up);
      ls.set("height", Math.round(dock.getBoundingClientRect().height)); };
    addEventListener("pointermove", move); addEventListener("pointerup", up);
  });
  setDockTab(ls.get("tab", "console"), false);
  setDockOpen(ls.get("open", false));
})();

// ---------- setup wizard (Settings > "Setup wizard" button; opens pre-filled from current settings) ----------
const W_STEPS = ["About you", "Job search", "Resume", "AI engine"];
const W_PREFS = ["target_titles", "title_must_include_any", "title_role_any", "title_exclude", "keywords_exclude",
                 "keywords_boost", "locations", "remote_only", "posted_within_days"];
const W_APP = ["applicant_name", "applicant_email", "applicant_phone", "applicant_location", "applicant_links"];
const W_SECRETS = { jsearch: ["jsearch_api_key"], usajobs: ["usajobs_api_key", "usajobs_email"] };
const W_KEY_LABELS = { jsearch_api_key: "JSearch API key", usajobs_api_key: "USAJOBS API key", usajobs_email: "USAJOBS account email" };
const W_KEY_HELP = {
  jsearch_api_key: "Free key: openwebninja.com -> free plan (no card) -> enable JSearch -> copy your API key. A RapidAPI \"JSearch\" key also works.",
  usajobs_api_key: "Free key: developer.usajobs.gov -> \"Get an API key\" (approval is usually instant).",
  usajobs_email: "The email that goes with your USAJOBS key - the API requires it to match.",
};
const wHasKey = (n) => (S.settings.secrets || []).some((x) => x.name === n && x.is_set);

function renderWKeys() {
  const need = $$("#w-sources input:checked").flatMap((c) => (W_SECRETS[c.value] || []).filter((n) => !wHasKey(n)));
  $("#w-source-keys").innerHTML = [...new Set(need)].map((n) =>
    `<label>${esc(W_KEY_LABELS[n])} * <span class="hint">${esc(W_KEY_HELP[n])}</span>
     <input class="wkey" data-secret="${n}" type="text" autocomplete="off" spellcheck="false"></label>`).join("");
}

function wizardGoto(i) {
  S.wstep = i;
  $$("#wizard-form .wstep").forEach((s) => s.classList.toggle("hidden", +s.dataset.wstep !== i));
  $$("#wizard-dots i").forEach((d, j) => d.classList.toggle("on", j <= i));
  $("#w-step-name").textContent = `${i + 1} / 4 · ${W_STEPS[i]}`;
  $("#w-back").classList.toggle("hidden", i === 0);
  $("#w-next").classList.toggle("hidden", i === 3);
  $("#w-finish").classList.toggle("hidden", i !== 3);
}

async function openWizard() {
  const f = $("#wizard-form"), a = S.settings.app, p = S.settings.preferences;
  $("#w-sources").innerHTML = S.settings.sources.map((s) =>
    `<label class="check"><input type="checkbox" value="${esc(s.id)}" ${(p.enabled_sources || []).includes(s.id) ? "checked" : ""}> ${esc(s.label)}${s.needs.length ? (s.ready ? ' <span class="badge">key saved</span>' : ' <span class="badge gray">key required</span>') : ""}</label>`).join("");
  renderWKeys();
  fillForm(f, { ...p, ...a });
  const ms = p.min_salary || 0;
  $("#w-pay-amount").value = ms > 0 ? ms : "";
  $("#w-pay-unit").value = ms > 0 ? "year" : "hour";
  const masters = (S.resumes.length ? S.resumes : (S.resumes = await api("/api/resumes"))).filter((r) => r.is_master);
  $("#w-resume-existing").textContent = masters.length
    ? `Master resume already on file: "${masters[0].name}". Uploading below replaces it.`
    : "No resume yet — the job scores, cover letters and tailored resumes all need one.";
  $("#w-resume-status").textContent = ""; $("#w-resume-file").value = "";
  const tier = $("#w-tier");
  if (!S.localModel?.tiers?.length) await loadLocalModel();
  tier.innerHTML = (S.localModel?.tiers || []).map((t) =>
    `<option value="${t.id}"${t.id === S.localModel.recommended ? " selected" : ""}>${esc(t.label)} — ${t.gb} GB${t.installed ? " (installed)" : ""}</option>`).join("");
  const tierBtn = $("#w-tier-btn"), tierLabel = () => {
    const t = (S.localModel?.tiers || []).find((x) => x.id === tier.value);
    tierBtn.textContent = t?.installed ? "Use this model" : `Install & use (${t?.gb ?? "?"} GB download)`;
  };
  tier.addEventListener("change", tierLabel); tierLabel();
  const builtin = a.ai_engine === "local_builtin";
  f.elements.w_ai.value = a.ai_engine === "claude_code" ? "claude_code"
    : a.ai_engine === "api" ? "api" : builtin ? "builtin" : "remote";
  $("#w-llm-url").value = a.local_llm_url || "";
  $("#w-llm-model").value = a.local_llm_model || "";
  $("#w-apimodel").value = a.model || "claude-opus-5-5";
  $("#w-llm-status").textContent = $("#w-key-status").textContent = "";
  $("#w-cc-status").textContent = S.claudeCode?.installed ? "Claude Code found on this machine." : "Claude Code not found — install it or pick another engine.";
  $("#w-apikey").placeholder = (S.settings.secrets || []).some((s) => s.name === "anthropic_api_key" && s.is_set)
    ? "saved (type to replace)" : "Anthropic API key (sk-ant-…)";
  wizardGoto(0);
  $("#wizard-dialog").showModal();
  api("/api/settings/wizard-seen", { method: "POST" }).catch(() => {});
}

function wWaiBody(wai) {
  const f = $("#wizard-form");
  if (wai === "remote") return { ai_engine: "local_external", local_llm_url: $("#w-llm-url").value.trim(), local_llm_model: $("#w-llm-model").value.trim() };
  if (wai === "api") return { ai_engine: "api", model: $("#w-apimodel").value };
  if (wai === "claude_code") return { ai_engine: "claude_code", claude_code_enabled: true };
  return { ai_engine: "local_builtin" };
}

async function wizardFinish() {
  const f = $("#wizard-form");
  const get = (n) => f.elements[n], val = (n) => { const el = get(n); return el.type === "checkbox" ? el.checked : el.value; };
  const prefs = {};
  for (const n of W_PREFS) prefs[n] = PREF_LISTS.includes(n) && n !== "locations" ? lines(get(n).value)
    : n === "locations" ? lines(get(n).value)
    : n === "min_salary" || n === "posted_within_days" ? Number(get(n).value || 0) : get(n).checked;
  prefs.enabled_sources = $$("#w-sources input:checked").map((c) => c.value);
  const amt = parseFloat($("#w-pay-amount").value || 0);
  prefs.min_salary = amt > 0 ? Math.round($("#w-pay-unit").value === "hour" ? amt * 2080 : amt) : 0;
  const app = {};
  for (const n of W_APP) app[n] = val(n);
  Object.assign(app, wWaiBody(f.elements.w_ai.value));
  const key = $("#w-apikey").value.trim();
  if (key) await api("/api/settings/secrets/anthropic_api_key", { method: "PUT", body: { value: key } });
  for (const i of $$("#w-source-keys .wkey"))
    if (i.value.trim()) await api(`/api/settings/secrets/${i.dataset.secret}`, { method: "PUT", body: { value: i.value.trim() } });
  await api("/api/settings/preferences", { method: "PUT", body: prefs });
  S.settings.app = await api("/api/settings/app", { method: "PUT", body: app });
  S.settings.preferences = prefs;
  $("#wizard-dialog").close();
  await loadSettings();
  S.hasResume = (await api("/api/resumes")).length > 0; renderBanner();
  toast("Setup saved. Happy hunting!");
}

$("#w-next").addEventListener("click", () => {
  if (S.wstep === 1) {
    const miss = $$("#w-source-keys .wkey").filter((i) => !i.value.trim());
    if (miss.length) { miss[0].focus(); miss[0].scrollIntoView({ block: "center" }); toast("Enter the key for each source you selected (or untick it) to continue.", true); return; }
  }
  wizardGoto(Math.min(3, S.wstep + 1));
});
$("#w-sources").addEventListener("change", renderWKeys);
$("#w-back").addEventListener("click", () => wizardGoto(Math.max(0, S.wstep - 1)));
$("#w-skip").addEventListener("click", () => $("#wizard-dialog").close());
$("#w-finish").addEventListener("click", (e) => busy(e.target, "Saving…", wizardFinish));
$("#wizard-form").addEventListener("submit", (e) => e.preventDefault());
$("#open-wizard").addEventListener("click", openWizard);
$("#w-resume-file").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  const st = $("#w-resume-status");
  try {
    st.textContent = "Uploading…";
    const fd = new FormData();
    fd.append("file", e.target.files[0]); fd.append("name", "");
    fd.append("cleanup", $("#w-resume-cleanup").checked); fd.append("make_master", "true");
    const r = await api("/api/resumes/upload", { method: "POST", body: fd });
    S.resumes = await api("/api/resumes");
    $("#w-resume-existing").textContent = `Master resume on file: "${S.resumes.find((x) => x.id === r.id)?.name || r.id}".`;
    st.textContent = r.warning ? "⚠ " + r.warning : "✓ Resume uploaded and set as master.";
    e.target.value = "";
  } catch (err) { st.textContent = "✗ " + err.message; }
});
$("#w-tier-btn").addEventListener("click", (e) => {
  const sel = $("#w-tier"), st = $("#w-lm-status");
  busy(e.target, "Working…", async () => {
    st.textContent = "Starting…";
    await api("/api/local-model/use", { method: "POST", body: { tier: sel.value } });
    for (let n = 0; n < 400; n++) {
      await new Promise((r) => setTimeout(r, 2500));
      const s = await api("/api/local-model/status").catch(() => null);
      if (!s) continue;
      if (s.install?.active) st.textContent = `Downloading (${s.install.phase}): ${s.install.total ? Math.floor(s.install.done / s.install.total * 100) + "%" : "…"}`;
      else if (s.server?.running && s.server.tier === sel.value) { st.textContent = "✓ Running — Finish to save."; loadLocalModel(); return; }
      else if (s.install?.phase === "error") { st.textContent = "✗ " + (s.install.error || "install failed"); return; }
      else if (s.server?.running) { st.textContent = "✓ Running."; return; }
    }
    st.textContent = "Still working — check Settings > AI Settings for progress, then Finish.";
  });
});
$("#w-llm-test").addEventListener("click", (e) => {
  const st = $("#w-llm-status");
  busy(e.target, "Checking…", async () => {
    await api("/api/settings/app", { method: "PUT", body: wWaiBody("remote") }).catch(() => {});
    const l = await api("/api/local-llm/status").catch(() => ({ reachable: false, error: "status check failed" }));
    st.innerHTML = l.reachable ? `<span class="state set">✓ reachable</span> loaded: ${(l.models || []).join(", ")}` : `<span style="color:var(--bad)">✗ ${esc(l.error || "unreachable")}</span>`;
  });
});

// ---------- boot ----------
$$("[data-go]").forEach((el) => el.addEventListener("click", () => goToTarget(el.dataset.go)));
(async function boot() {
  try {
    const env = await api("/api/env");
    document.title = env.app_name;
    $("link[rel=icon]").href = `/static/${env.icon}.png`;
    if (env.env === "staging") {
      document.body.classList.add("staging");
      $(".brand").innerHTML = `JobScout <span class="env-tag">STAGING</span>`;
      const strip = document.createElement("div");
      strip.className = "staging-strip";
      strip.textContent = "Staging: test build. Its data, keys and Gmail connection are separate from production.";
      document.body.prepend(strip);
      $("#staging-tools").classList.remove("hidden");
      $("#copy-prod").addEventListener("click", (e) => {
        if (!confirm("Replace staging's data with a copy of production's?")) return;
        busy(e.target, "Copying…", async () => {
          const r = await api("/api/env/copy-production", { method: "POST" });
          toast("Copied: " + r.copied.join(", "));
          setTimeout(() => location.reload(), 1500);
        });
      });
    }
    await loadSettings();
    S.hasResume = (await api("/api/resumes")).length > 0; renderBanner();
    await loadJobs(); updateMailBadge();
    if (S.settings.first_run) openWizard(); // new install: guide them through setup before anything else
    // The app does one refresh when it opens. Show its progress, then reload the lists once it's done.
    const s = await updateRunStatus();
    if (s?.running) {
      const poll = setInterval(async () => {
        const st = await updateRunStatus();
        if (st && !st.running) { clearInterval(poll); await loadJobs(); updateMailBadge(); }
      }, 3000);
    }
    setInterval(updateRunStatus, 60000); // only updates the "Last refreshed" label; fetches nothing
  } catch (e) { toast("Couldn't reach JobScout: " + e.message, true); }
})();
