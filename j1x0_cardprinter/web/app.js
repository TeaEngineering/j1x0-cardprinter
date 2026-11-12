"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  design: null,          // name of the chosen design
  values: {},            // field name -> text, or upload id for image fields
  renderId: null,        // the render the Print button would send
  rendering: false,
  stale: true,           // the form has changed since the last render
  seq: 0,                // ignore render responses that a newer request has superseded
  status: { state: "unknown", ready: false, message: "Checking the printer…" },
  job: null,
  timer: null,
  uploads: 0,            // photos still being uploaded
};

// ── API ─────────────────────────────────────────────────────────────────────

async function api(path, options = {}) {
  const headers = Object.assign({ "X-J1x0-Token": window.J1X0_TOKEN }, options.headers || {});
  const response = await fetch(path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    let message = response.statusText;
    try { message = (await response.json()).error || message; } catch (e) { /* not JSON */ }
    throw new Error(message);
  }
  return response;
}
const getJSON = async (path) => (await api(path)).json();
const postJSON = async (path, body) => (await api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
})).json();

// ── Designs ─────────────────────────────────────────────────────────────────

async function loadDesigns() {
  const data = await getJSON("/api/designs");
  const box = $("designs");
  box.replaceChildren();
  $("designs-dir").textContent = data.dir;
  $("no-designs").hidden = data.designs.length > 0;
  for (const design of data.designs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "design";
    button.dataset.name = design.name;
    button.setAttribute("aria-pressed", String(design.name === state.design));
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/api/designs/${encodeURIComponent(design.name)}/thumb.png`;
    img.onerror = () => { img.style.visibility = "hidden"; };
    const title = document.createElement("span");
    title.textContent = design.title;
    button.append(img, title);
    button.addEventListener("click", () => selectDesign(design.name));
    box.append(button);
  }
  const remembered = localStorage.getItem("j1x0.design");
  const names = data.designs.map((d) => d.name);
  if (!state.design || !names.includes(state.design)) {
    const pick = names.includes(remembered) ? remembered : (names.length === 1 ? names[0] : null);
    if (pick) selectDesign(pick);
  }
}

async function selectDesign(name) {
  let info;
  try {
    info = await getJSON(`/api/designs/${encodeURIComponent(name)}`);
  } catch (error) {
    showOverlay(error.message, true);
    $("print-panel").hidden = false;
    return;
  }
  state.design = name;
  state.values = {};
  state.renderId = null;
  state.stale = true;
  try { localStorage.setItem("j1x0.design", name); } catch (e) { /* private mode */ }
  for (const b of document.querySelectorAll(".design")) {
    b.setAttribute("aria-pressed", String(b.dataset.name === name));
  }
  buildForm(info);
  $("fill").hidden = false;
  $("print-panel").hidden = false;
  clearJobNote();
  render();
}

function buildForm(info) {
  const form = $("form");
  form.replaceChildren();
  if (info.fields.length === 0) {
    const p = document.createElement("p");
    p.textContent = "This design has nothing to fill in.";
    form.append(p);
  }
  info.fields.forEach((field, i) => {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const label = document.createElement("label");
    const id = `field-${i}`;
    label.htmlFor = id;
    label.textContent = field.label;
    const input = document.createElement("input");
    input.id = id;
    wrap.append(label, input);
    if (field.kind === "image") {
      input.type = "file";
      input.accept = "image/*";
      const thumb = document.createElement("img");
      thumb.className = "thumb";
      thumb.hidden = true;
      thumb.alt = "";
      wrap.append(thumb);
      input.addEventListener("change", () => uploadPhoto(field.name, input.files[0], thumb));
    } else {
      input.type = "text";
      input.addEventListener("input", () => changed(field.name, input.value));
    }
    form.append(wrap);
  });
  form.onsubmit = (event) => { event.preventDefault(); render(); };
}

function changed(name, value) {
  state.values[name] = value;
  state.stale = true;
  updateUI();
  scheduleRender();
}

async function uploadPhoto(fieldName, file, thumb) {
  if (!file) {
    changed(fieldName, "");
    thumb.hidden = true;
    return;
  }
  state.uploads += 1;
  state.stale = true;
  updateUI();
  try {
    const data = await (await api("/api/upload", {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
    })).json();
    thumb.src = URL.createObjectURL(file);
    thumb.hidden = false;
    state.values[fieldName] = data.id;
  } catch (error) {
    showOverlay(error.message, true);
  } finally {
    state.uploads -= 1;
  }
  changed(fieldName, state.values[fieldName] || "");
  render();
}

// ── Rendering ───────────────────────────────────────────────────────────────

function scheduleRender() {
  clearTimeout(state.timer);
  state.timer = setTimeout(render, 450);        // wait for a pause in typing
}

function showOverlay(message, isError) {
  const overlay = $("preview-overlay");
  overlay.textContent = message;
  overlay.className = isError ? "overlay error" : "overlay";
  overlay.hidden = !message;
}

async function render() {
  clearTimeout(state.timer);
  if (!state.design) return;
  const seq = ++state.seq;
  state.rendering = true;
  clearJobNote();
  if (!state.renderId) showOverlay("Drawing the card…", false);
  updateUI();
  try {
    const result = await postJSON("/api/render", {
      design: state.design, values: state.values,
    });
    if (seq !== state.seq) return;
    state.renderId = result.id;
    state.stale = false;
    $("preview").src = `/api/preview/${result.id}.png`;
    showOverlay("", false);
    const warnings = $("warnings");
    warnings.hidden = result.warnings.length === 0;
    warnings.textContent = result.warnings.join(". ");
  } catch (error) {
    if (seq !== state.seq) return;
    state.renderId = null;
    showOverlay(error.message, true);
  } finally {
    if (seq === state.seq) {
      state.rendering = false;
      updateUI();
    }
  }
}

// ── Printing ────────────────────────────────────────────────────────────────

function printState() {
  if (!state.design) return { ok: false, note: "Choose a design first." };
  if (state.job && state.job.state === "sending") return { ok: false, note: state.job.message };
  if (state.rendering || state.stale || state.uploads) return { ok: false, note: "Drawing the card…" };
  if (!state.renderId) return { ok: false, note: "The card could not be drawn." };
  if (!state.status.ready) {
    return { ok: false, note: state.status.message || "Waiting for the printer…" };
  }
  return { ok: true, note: "" };
}

function updateUI() {
  const { ok, note } = printState();
  $("print").disabled = !ok;
  const noteBox = $("print-note");
  if (!noteBox.classList.contains("ok") && !noteBox.classList.contains("bad")) {
    noteBox.textContent = note;
  }
  const chip = $("status");
  chip.className = `chip chip-${state.status.state}`;
  chip.textContent = state.status.message;
}

function clearJobNote() {
  const note = $("print-note");
  note.className = "note";
  note.textContent = "";
}

async function print() {
  clearJobNote();
  $("print").disabled = true;
  try {
    state.job = await postJSON("/api/print", { id: state.renderId });
  } catch (error) {
    state.job = { state: "error", message: error.message };
  }
  await pollJob();
}

async function pollJob() {
  while (state.job && state.job.state === "sending") {
    $("print-note").textContent = state.job.message;
    updateUI();
    await new Promise((resolve) => setTimeout(resolve, 600));
    try { state.job = await getJSON("/api/job"); } catch (e) { state.job = { state: "error", message: e.message }; }
  }
  const note = $("print-note");
  note.className = state.job.state === "done" ? "note ok" : "note bad";
  note.textContent = state.job.message;
  updateUI();
  refreshStatus();
}

// ── Printer status ──────────────────────────────────────────────────────────

async function refreshStatus() {
  if (state.job && state.job.state === "sending") return;
  try {
    state.status = await getJSON("/api/status");
  } catch (error) {
    state.status = { state: "error", ready: false, message: error.message };
  }
  updateUI();
}

// The details live in a popover under the status badge: click the badge to open or close it.
async function openDetails() {
  const pop = $("details-pop");
  const out = $("details-out");
  pop.hidden = false;
  $("status").setAttribute("aria-expanded", "true");
  out.textContent = "Asking the printer…";
  try {
    const data = await getJSON("/api/status?full=1");
    if (pop.hidden) return;                      // closed while we were asking
    out.textContent = (data.lines && data.lines.length ? data.lines.join("\n") : data.message);
  } catch (error) {
    out.textContent = error.message;
  }
}

function closeDetails() {
  $("details-pop").hidden = true;
  $("status").setAttribute("aria-expanded", "false");
}

function toggleDetails() {
  if ($("details-pop").hidden) openDetails(); else closeDetails();
}

// ── Uploading a template ────────────────────────────────────────────────────

function uploadNote(message, kind) {
  const note = $("upload-note");
  note.textContent = message;
  note.className = kind ? `upload-note ${kind}` : "upload-note";
}

async function uploadTemplate(file) {
  if (!file) return;
  uploadNote(`Uploading ${file.name}…`);
  $("upload-template").disabled = true;
  try {
    const result = await (await api("/api/designs", {
      method: "POST",
      headers: { "Content-Type": "image/svg+xml", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    })).json();
    await loadDesigns();
    await selectDesign(result.name);
    uploadNote(`${result.replaced ? "Updated" : "Added"} “${result.title}”.`, "ok");
  } catch (error) {
    uploadNote(error.message, "bad");
  } finally {
    $("upload-template").disabled = false;
    $("template-file").value = "";               // so the same file can be chosen again
  }
}

// ── Start ───────────────────────────────────────────────────────────────────

$("print").addEventListener("click", print);
$("reload").addEventListener("click", loadDesigns);
$("status-refresh").addEventListener("click", refreshStatus);
$("status").addEventListener("click", toggleDetails);
document.addEventListener("click", (event) => {
  if (!$("details-pop").hidden && !event.target.closest(".status-wrap")) closeDetails();
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDetails(); });
$("upload-template").addEventListener("click", () => $("template-file").click());
$("template-file").addEventListener("change", () => uploadTemplate($("template-file").files[0]));

loadDesigns().catch((error) => showOverlay(error.message, true));
refreshStatus();
setInterval(() => { if (!document.hidden) refreshStatus(); }, 5000);
