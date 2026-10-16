"use strict";
const $ = (selector) => document.querySelector(selector);
const form = $("#settings");
const pendingModels = new Set(), modelRequestErrors = new Set(), modelSignatures = new Map(), effortSignatures = new Map();
const commandChecks = new Map(), handledChecks = new Set();
// Model picker values: "" is the CLI's own default; Core strips saved models, so no model ID starts with a space.
const CUSTOM_MODEL = " custom";
let warningCommand = null;
let state,
  saved = new Map(),
  dirty = false,
  editRevision = 0,
  initialized = false,
  stopped = false;
const numeric = new Set([
  "timeout_seconds",
  "high_timeout_seconds",
  "max_operations",
  "world_concurrency",
  "world_max_pending",
  "world_max_jobs_per_player",
  "world_lease_seconds",
  "port",
  "world_port",
]);
function notify(message, error = false) {
  const element = $("#notice");
  element.hidden = false;
  $("#notice-text").textContent = message;
  element.classList.toggle("error", error);
}
$("#notice-close").addEventListener("click", () => { $("#notice").hidden = true; });
function selectedProvider() {
  return form.elements.provider.value;
}
function markDirty() {
  editRevision += 1;
  updateChanges();
}
/** Current value of every settings control, keyed by name (or id for unnamed inputs). */
function formSnapshot() {
  const values = new Map();
  for (const input of form.elements) {
    const key = input.name || input.id;
    if (!key || input.type === "submit" || input.type === "button" || input.tagName === "FIELDSET") continue;
    if (input.type === "radio") { if (input.checked) values.set(key, input.value); }
    else values.set(key, input.type === "checkbox" ? input.checked : input.value);
  }
  return values;
}
/** Outline each control that differs from the saved settings and summarize them by the save buttons. */
function updateChanges() {
  const current = formSnapshot();
  const changed = new Set([...current.keys()].filter(key => current.get(key) !== saved.get(key)));
  for (const input of form.elements) {
    const key = input.name || input.id;
    if (!key || !current.has(key) && input.type !== "radio") continue;
    const target = input.type === "radio" ? input.closest(".provider-grid") : input.closest("label") || input;
    target.classList.toggle("changed", changed.has(key));
  }
  dirty = changed.size > 0;
  document.body.classList.toggle("has-changes", dirty);
  // Advanced settings sit outside the form element and join it through form="settings".
  for (const panel of document.querySelectorAll(".panel")) {
    const panelChanged = !!panel.querySelector(".changed");
    panel.classList.toggle("has-changes", panelChanged);
    document.querySelector(`.nav-item[href="#${panel.id}"]`)?.classList.toggle("has-changes", panelChanged);
  }
  const hint = dirty ? `${changed.size} unsaved change${changed.size === 1 ? "" : "s"}` : "";
  document.querySelectorAll(".save-hint").forEach(element => { element.textContent = hint; });
  $("#world-port-warning").hidden = !changed.has("world_port");
}
function readCommand(provider) {
  const text = form.elements[provider + "_command"].value.trim();
  let command;
  try { command = text ? JSON.parse(text) : []; }
  catch { throw new Error("Enter a JSON array of command arguments, or leave the field empty to find the CLI automatically."); }
  if (!Array.isArray(command) || command.length > 16 || command.some(arg =>
    typeof arg !== "string" || !arg.trim() || arg.includes("\0") || arg.length > 4096)) {
    throw new Error("Enter a JSON array of non-empty command arguments, or leave the field empty to find the CLI automatically.");
  }
  return command;
}
function warnCommand(provider, message) {
  warningCommand = { provider, input: form.elements[provider + "_command"].value };
  $("#command-warning-message").textContent = `${provider === "codex" ? "Codex" : "Claude"}: ${message} Choose Find automatically to replace the command with a working installation, or OK to keep it.`;
  if (!$("#command-warning").open) $("#command-warning").showModal();
}
async function checkCommand(provider, automatic = false) {
  const input = form.elements[provider + "_command"].value;
  let command;
  try { command = automatic ? [] : readCommand(provider); }
  catch (error) { warnCommand(provider, error.message); return; }
  const pending = { input };
  commandChecks.set(provider, pending);
  try {
    const result = await post("/api/providers/check", { provider, command });
    if (commandChecks.get(provider) !== pending) return;
    pending.id = result.providers[provider]?.check_id;
    state = result;
    render();
  } catch (error) {
    if (commandChecks.get(provider) === pending) commandChecks.delete(provider);
    throw error;
  }
}
function renderCommand(provider) {
  const status = state.providers[provider];
  if (!status?.check_id || status.checking || handledChecks.has(status.check_id)) return;
  const pending = commandChecks.get(provider);
  // A check request may still be awaiting its HTTP response while polling completes.
  if (pending && pending.id !== status.check_id) return;
  handledChecks.add(status.check_id);
  commandChecks.delete(provider);
  const input = form.elements[provider + "_command"];
  if (pending) {
    if (input.value !== pending.input) return;
  } else {
    try { if (JSON.stringify(readCommand(provider)) !== JSON.stringify(status.requested_command)) return; }
    catch { return; }
  }
  if (status.ready && !status.requested_command.length && status.command?.length) {
    input.value = JSON.stringify(status.command);
    markDirty();
    notify("CLI found and checked. Save settings to keep this command.");
  } else if (!status.ready && status.requested_command.length) {
    warnCommand(provider, status.message);
  } else if (!status.ready) {
    notify(status.message, true);
  }
}
$("#command-warning-ok").addEventListener("click", () => $("#command-warning").close());
$("#command-warning-find").addEventListener("click", () => {
  const warning = warningCommand;
  $("#command-warning").close();
  if (warning && form.elements[warning.provider + "_command"].value === warning.input) {
    action(() => checkCommand(warning.provider, true));
  }
});
function showProvider() {
  const provider = selectedProvider();
  document
    .querySelectorAll("[data-provider]")
    .forEach((e) => (e.hidden = e.dataset.provider !== provider));
  $("#provider-message").textContent =
    state?.providers[provider]?.message ||
    "The CLI keeps your credentials.";
  for (const name of ["codex", "claude"]) {
    const custom = form.elements[name + "_model"].value === CUSTOM_MODEL;
    $(`#${name}-custom-label`).hidden = !custom;
    $(`#${name}-custom-model`).disabled = !custom;
    $(`#${name}-custom-model`).required = custom;
  }
}
function renderModels(provider, savedModel) {
  const select = form.elements[provider + "_model"];
  const catalog = state.models?.[provider];
  const items = catalog?.items || [];
  const signature = JSON.stringify(items);
  if (savedModel !== undefined || modelSignatures.get(provider) !== signature) {
    const current = savedModel ?? select.value;
    const options = [new Option("CLI default", ""), ...items.map(model => {
      const identity = model.resolved_model || model.id;
      const label = model.name === identity ? identity : `${model.name} (${identity})`;
      const option = new Option(label + (model.supports_images ? "" : " — no image support"), model.id);
      option.disabled = !model.supports_images;
      return option;
    })];
    if (current && !items.some(model => model.id === current)) {
      options.splice(1, 0, new Option(`${current} (saved model)`, current));
    }
    options.push(new Option("Custom model…", CUSTOM_MODEL));
    select.replaceChildren(...options);
    select.value = current;
    modelSignatures.set(provider, signature);
  }
  $(`[data-refresh-models="${provider}"]`).disabled = !!catalog?.loading;
  $(`#${provider}-models-message`).textContent = catalog?.message || "Models load from your configured CLI.";
}
function effortLabel(level) {
  return level === "xhigh" ? "Extra high" : level[0].toUpperCase() + level.slice(1);
}
function renderEfforts(provider, { saved, modelChanged = false } = {}) {
  const select = form.elements[provider + "_reasoning_effort"];
  const items = state.models?.[provider]?.items || [];
  const selected = form.elements[provider + "_model"].value;
  const id = selected === CUSTOM_MODEL ? $(`#${provider}-custom-model`).value.trim() : selected;
  const model = items.find(item => item.id === id);
  // The CLI's own order, low to high, across every model it listed.
  const reported = [...new Set(items.flatMap(item => item.reasoning_efforts || []))];
  // A custom or unlisted model may use any level the CLI reports for its listed models.
  const levels = model ? model.reasoning_efforts || [] : reported;
  let current = saved ?? select.value;
  if (modelChanged && model && levels.length && !levels.includes(current)) {
    const lower = reported.slice(0, Math.max(0, reported.indexOf(current))).reverse().find(level => levels.includes(level));
    current = lower ?? (levels.includes(model.default_reasoning_effort) ? model.default_reasoning_effort : levels[0]);
  }
  const signature = JSON.stringify([levels, current]);
  if (effortSignatures.get(provider) !== signature) {
    const options = levels.map(level => new Option(effortLabel(level), level));
    if (current && !levels.includes(current)) options.unshift(new Option(effortLabel(current) + (items.length ? " (not listed)" : ""), current));
    select.replaceChildren(...options);
    select.value = current;
    effortSignatures.set(provider, signature);
  }
  $(`#${provider}-efforts-message`).textContent = !items.length
    ? "Levels load with the model list."
    : !model
      ? "Showing every level your CLI reports. Your CLI decides whether this model accepts it."
      : levels.length
        ? "Levels reported by your CLI for this model."
        : "Your CLI reports no reasoning effort levels for this model.";
}
async function refreshModels(provider) {
  if (pendingModels.has(provider)) return;
  pendingModels.add(provider);
  try {
    state = await post("/api/providers/models", { provider, command: readCommand(provider) });
    modelRequestErrors.delete(provider);
    render();
  } catch (error) {
    modelRequestErrors.add(provider);
    notify(error.message, true);
  } finally {
    pendingModels.delete(provider);
  }
}
function fill(config) {
  for (const provider of ["codex", "claude"]) {
    renderModels(provider, config[provider + "_model"]);
    renderEfforts(provider, { saved: config[provider + "_reasoning_effort"] });
  }
  for (const [key, value] of Object.entries(config)) {
    const input = form.elements.namedItem(key);
    if (input?.type === "checkbox") input.checked = value;
    else if (input)
      input.value = Array.isArray(value) ? (value.length ? JSON.stringify(value) : "") : value;
  }
  showProvider();
  showJobLimits();
  saved = formSnapshot();
  updateChanges();
}
/** Job limit fields are grayed out while the limits are off; disabled fields are not submitted. */
function showJobLimits() {
  const off = !form.elements.job_limits.checked;
  $("#job-limit-fields").classList.toggle("off", off);
  for (const input of $("#job-limit-fields").querySelectorAll("input")) input.disabled = off;
}
form.elements.job_limits.addEventListener("change", showJobLimits);
function readConfig() {
  const config = { ...state.config };
  for (const [key, value] of new FormData(form)) {
    config[key] = numeric.has(key)
      ? Number(value)
      : key.endsWith("_command")
        ? readCommand(key.slice(0, -8))
        : value;
  }
  for (const input of form.querySelectorAll('input[type="checkbox"]')) {
    config[input.name] = input.checked;
  }
  for (const provider of ["codex", "claude"]) {
    if (config[provider + "_model"] === CUSTOM_MODEL) config[provider + "_model"] = $(`#${provider}-custom-model`).value.trim();
  }
  return config;
}
const access = new URLSearchParams(window.location.hash.slice(1)).get("session");
if (access) {
  sessionStorage.setItem("worldprompt-session", access);
  history.replaceState(null, "", window.location.pathname + window.location.search);
}
const sessionToken = access || sessionStorage.getItem("worldprompt-session") || "";
async function post(path, data = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-WorldPrompt-Session": sessionToken,
    },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The action failed.");
  return result;
}
async function save() {
  if (!form.reportValidity()) throw new Error("Please check your settings.");
  const revision = editRevision, submitted = formSnapshot();
  state = await post("/api/config", readConfig());
  // Saving invalidates checks started with the previous server configuration.
  commandChecks.clear();
  if (editRevision === revision) fill(state.config);
  else { saved = submitted; updateChanges(); }
  render();
}
const WORLD_STATUS = {
  running: "Ready for the mod",
  stopping: "Stopping",
  offline: "Not reachable",
  stopped: "Core is stopped",
};
function serviceStatus(status, text) {
  const pill = $("#service-status");
  pill.dataset.state = status;
  pill.textContent = text;
  // The Minecraft connection bar follows the same Core service state.
  $(".connection-bar").dataset.state = status;
  $("#world-status").textContent = WORLD_STATUS[status];
}
function render() {
  $("#version").textContent = `v${state.version}`;
  $("#footer-version").textContent = state.version;
  $("#world-url").textContent = state.service.world_url;
  $("#config-path").textContent = state.config_path;
  $("#restart").hidden = !state.restart_required;
  serviceStatus(state.service.stopping ? "stopping" : "running",
    state.service.stopping ? "Stopping" : "Core is running");
  for (const provider of ["codex", "claude"]) {
    const status = state.providers[provider];
    const el = $(`#${provider}-status`);
    el.textContent = status?.checking
      ? "Checking…"
      : status?.ready
        ? "Account connected"
        : status
          ? "Connection needed"
          : "Not checked";
    el.classList.toggle("ready", !!status?.ready);
    renderModels(provider);
    renderEfforts(provider);
    renderCommand(provider);
  }
  showProvider();
  const provider = selectedProvider();
  if (!state.providers[provider] && !commandChecks.has(provider)) action(() => checkCommand(provider));
  $("#check-provider").disabled = !!state.providers[provider]?.checking || commandChecks.has(provider);
  renderJobs();
}
const jobRows = new Map();
let cleaningJobs = false;
function jobText(row, selector, value) {
  const element = row.querySelector(selector);
  if (element.textContent !== value) element.textContent = value;
}
function duration(seconds) {
  if (!Number.isFinite(seconds)) return "Unknown";
  const total = Math.floor(seconds);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor(total % 3600 / 60)}m`;
}
function jobDate(timestamp) {
  return Number.isFinite(timestamp) ? new Date(timestamp * 1000).toLocaleString() : "Unknown";
}
function renderJobs() {
  const recent = state.recent_jobs || [];
  const finished = state.recent_job_count ?? recent.length;
  $("#job-count").textContent = `${state.jobs.length} ACTIVE · ${finished} FINISHED`;
  $("#history-hint").textContent = finished > recent.length
    ? `Showing the latest ${recent.length} of ${finished} finished jobs. Clear finished includes older history too.`
    : "Finished jobs keep their prompts, captures, previews and plans on this computer.";
  $("#clear-jobs").disabled = cleaningJobs || !state.cleanup_job_ids?.length;
  const jobs = $("#jobs");
  const visibleJobs = [...state.jobs, ...recent];
  const ids = new Set(visibleJobs.map(job => job.id));
  for (const [id, row] of jobRows) {
    if (!ids.has(id)) { row.remove(); jobRows.delete(id); }
  }
  if (visibleJobs.length) jobs.querySelector(".empty")?.remove();
  else if (!jobs.querySelector(".empty")) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No recent jobs. Use /wp prompt after selecting two corners.";
    jobs.append(empty);
  }
  visibleJobs.forEach((job, index) => {
    let row = jobRows.get(job.id);
    if (!row) {
      row = document.createElement("article");
      row.className = "job";
      // Only this fixed template uses HTML; job content is always plain text.
      row.innerHTML = `<div class="job-heading"><h3 class="job-prompt"></h3><span class="tag job-status"></span></div>
        <p class="job-context"></p><p class="job-settings"></p><p class="job-message"></p>
        <p class="job-metrics"></p><details class="job-summary" hidden><summary>Model summary</summary>
        <p class="job-summary-text"></p></details><details class="job-details"><summary>Job details</summary>
        <p class="job-full-prompt"></p><dl>
        <dt>Job ID</dt><dd class="job-id"></dd><dt>Started</dt><dd class="job-started"></dd>
        <dt>Finished</dt><dd class="job-finished"></dd><dt>Selection</dt><dd class="job-selection"></dd>
        <dt>Dimension</dt><dd class="job-location"></dd>
        <dt>Pos1 (min)</dt><dd class="job-pos1"></dd><dt>Pos2 (max)</dt><dd class="job-pos2"></dd>
        <dt>Model settings</dt><dd class="job-model"></dd>
        <dt>Tokens</dt><dd class="job-tokens"></dd>
        <dt>Prompt lecterns</dt><dd class="job-lecterns"></dd>
        </dl></details><div class="job-cleanup"><span class="job-cleanup-hint hint"></span>
        <button type="button" class="text-button remove-job">Remove job</button></div>`;
      row.querySelector(".remove-job").addEventListener("click", () => action(() => removeJobs([job.id])));
      jobRows.set(job.id, row);
    }
    // Keep existing nodes so polling preserves keyboard focus and open details.
    if (jobs.children[index] !== row) jobs.insertBefore(row, jobs.children[index] || null);
    const terminal = ["completed", "failed"].includes(job.status);
    const prompt = job.prompt || "Prompt unavailable for this older job";
    const mode = job.effort === "high" ? "Double pass" : "Single pass";
    const provider = {codex: "Codex", claude: "Claude"}[job.provider] || "Unknown provider";
    const model = `${provider} · ${job.model || "Unknown model"}`;
    const settings = `${model} · ${job.reasoning_effort || "Unknown"} reasoning · Fast mode ${job.fast_mode === true ? "on" : job.fast_mode === false ? "off" : "unknown"}`;
    const dims = job.dimensions;
    const selection = Array.isArray(dims) && dims.length === 3
      ? `${dims.join(" × ")} blocks (X × Y × Z) · ${dims.reduce((a, b) => a * b, 1).toLocaleString()} total`
      : "Unknown";
    const usage = job.token_usage || {};
    const tokens = usage.total_tokens == null ? "Tokens unavailable"
      : `${usage.total_tokens.toLocaleString()}${usage.complete ? "" : "+"} tokens${usage.complete ? "" : " (partial)"}`;
    jobText(row, ".job-prompt", prompt);
    jobText(row, ".job-status", job.status === "reviewing" ? "reviewing and refining" : job.status);
    row.querySelector(".job-status").dataset.status = job.status;
    jobText(row, ".job-context", `${job.owner_name} · ${mode} · ${jobDate(job.started_at)}`);
    jobText(row, ".job-settings", model);
    jobText(row, ".job-message", job.message || "");
    jobText(row, ".job-metrics", `${duration(job.elapsed_seconds)} · ${tokens} · ${job.operation || 0}/${job.operation_count || 0} operations${job.queue_position ? ` · Queue #${job.queue_position}` : ""}`);
    const summary = typeof job.summary === "string" ? job.summary.trim() : "";
    row.querySelector(".job-summary").hidden = !summary;
    jobText(row, ".job-summary-text", summary);
    jobText(row, ".job-full-prompt", prompt);
    jobText(row, ".job-id", job.id);
    jobText(row, ".job-started", jobDate(job.started_at));
    jobText(row, ".job-finished", terminal ? jobDate(job.finished_at) : "In progress");
    jobText(row, ".job-selection", selection);
    jobText(row, ".job-location", job.dimension || "Not recorded");
    const origin = job.selection_origin;
    const cornersKnown = Array.isArray(origin) && origin.length === 3 && Array.isArray(dims) && dims.length === 3;
    const coordinates = values => `X ${values[0]} · Y ${values[1]} · Z ${values[2]}`;
    jobText(row, ".job-pos1", cornersKnown ? coordinates(origin) : "Not recorded");
    jobText(row, ".job-pos2", cornersKnown ? coordinates(origin.map((n, axis) => n + dims[axis] - 1)) : "Not recorded");
    jobText(row, ".job-model", settings);
    jobText(row, ".job-tokens", `${tokens} · Input: ${usage.input_tokens?.toLocaleString() ?? "unknown"} · Output: ${usage.output_tokens?.toLocaleString() ?? "unknown"}`);
    const lecterns = Array.isArray(job.lecterns) ? job.lecterns : null;
    jobText(row, ".job-lecterns", !lecterns ? "Not recorded" : !lecterns.length ? "None"
      : lecterns.map(item => `${Array.isArray(item.world_position) ? `(${item.world_position.join(", ")}) · ` : ""}local [${item.position.join(", ")}]\n${item.text}`).join("\n\n"));
    row.querySelector(".job-cleanup").hidden = !terminal;
    jobText(row, ".job-cleanup-hint", job.cleanup_blocked_reason || "");
    const remove = row.querySelector(".remove-job");
    remove.disabled = cleaningJobs || !!job.cleanup_blocked_reason;
    remove.setAttribute("aria-label", `Remove job: ${prompt.slice(0, 100)}`);
  });
}
async function removeJobs(ids) {
  if (cleaningJobs || !ids.length) return;
  const count = ids.length;
  if (!window.confirm(`Permanently remove ${count === 1 ? "this finished job" : `${count} finished jobs, including older history`} and their stored prompts, captures, previews, logs and plans? Minecraft keeps its own plan for rebuilding and its undo snapshot until the server stops. This does not change blocks in Minecraft.`)) return;
  cleaningJobs = true;
  renderJobs();
  try {
    const result = await post("/api/jobs/remove", { job_ids: ids });
    state = result.state;
    notify(`Removed ${result.removed} finished job${result.removed === 1 ? "" : "s"} and their stored artifacts.`);
  } finally {
    cleaningJobs = false;
    renderJobs();
  }
}
$("#clear-jobs").addEventListener("click", () => action(() => removeJobs([...(state.cleanup_job_ids || [])])));

async function poll() {
  if (stopped) return;
  try {
    const response = await fetch("/api/state", { headers: { "X-WorldPrompt-Session": sessionToken } });
    if (!response.ok) throw new Error();
    state = await response.json();
    if (!initialized) {
      fill(state.config);
      initialized = true;
    }
    render();
  } catch {
    serviceStatus("offline", sessionToken ? "Core is offline or access expired" : "Open the private Core settings URL");
  } finally {
    if (!stopped) setTimeout(poll, 1800);
  }
}
async function action(fn) {
  try {
    await fn();
  } catch (error) {
    notify(error.message, true);
  }
}
document.addEventListener("input", event => {
  if (event.target.form !== form) return;
  markDirty();
  const provider = ["codex", "claude"].find(name =>
    event.target === form.elements[name + "_model"] || event.target.id === `${name}-custom-model`);
  if (provider) renderEfforts(provider, { modelChanged: true });
  showProvider();
  // The model choice can reset its effort, so compare after the dependent controls update.
  updateChanges();
});
document.addEventListener("change", event => {
  if (event.target.form !== form) return;
  updateChanges();
  if (event.target.name === "provider") action(() => checkCommand(selectedProvider()));
});
window.addEventListener("beforeunload", event => {
  if (dirty && !stopped) event.preventDefault();
});
form.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    await save();
    notify(
      dirty
        ? "Settings saved. Your newer edits are still unsaved."
        : state.restart_required
        ? "Settings saved. Restart Core to apply connection or storage changes."
        : "Settings saved. Future jobs will use these preferences.",
    );
  });
});
$("#check-provider").addEventListener("click", () =>
  action(() => checkCommand(selectedProvider())),
);
document.querySelectorAll("[data-refresh-models]").forEach(button => {
  button.addEventListener("click", () => action(async () => {
    await refreshModels(button.dataset.refreshModels);
  }));
});
$("#connect-game").addEventListener("click", () =>
  action(async () => {
    if (dirty) await save();
    const result = await post("/api/minecraft/connect", {
      folder: form.elements.minecraft_dir.value,
    });
    notify(result.message);
  }),
);
$("#stop-core").addEventListener("click", () =>
  action(async () => {
    if (
      state.jobs.length &&
      !window.confirm("Stop Core and cancel active Minecraft jobs?")
    )
      return;
    const result = await post("/api/shutdown");
    stopped = true;
    notify(result.message);
    serviceStatus("stopped", "Core is stopped");
    document.querySelectorAll("button").forEach((b) => (b.disabled = true));
  }),
);
// Highlight the sidebar entry of the section at the top of the window.
const navSections = [...document.querySelectorAll(".nav-item")].map(link => document.querySelector(link.getAttribute("href")));
function highlightNav() {
  const reading = navSections.filter(section => section.getBoundingClientRect().top < window.innerHeight * 0.35).pop() || navSections[0];
  document.querySelectorAll(".nav-item").forEach((link, index) => {
    if (navSections[index] === reading) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  });
}
window.addEventListener("scroll", highlightNav, { passive: true });
highlightNav();
$('.nav-item[href="#advanced"]').addEventListener("click", () => { $("#advanced").open = true; });
poll();
