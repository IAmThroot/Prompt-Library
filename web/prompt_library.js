import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { latestRequest, retainedResources, releaseMedia } from "./lifecycle.js";

const API = "/prompt_library";
const PAGE = 50;
const FILTERS = [
  ["all", "All"],
  ["favorites", "Favorites"],
  ["image", "Images"],
  ["video", "Videos"],
  ["audio", "Audio"],
  ["saved", "Saved by me"],
];
const SORTS = [
  ["latest", "Latest"],
  ["oldest", "Oldest"],
  ["added", "Recently added"],
];

const panels = new Set(); // every mounted library panel (sidebar and/or modal)
const overlays = []; // open modals, topmost last

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function call(path, { method = "GET", body, signal } = {}) {
  const res = await api.fetchApi(`${API}${path}`, {
    method,
    signal,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  if (!res.ok) throw new Error(data?.error || `Request failed (${res.status})`);
  return data;
}

let toastEl, toastTimer;
function notify(message, kind = "info") {
  if (!toastEl) {
    toastEl = h("div", { class: "pl-toast" });
    document.body.append(toastEl);
  }
  toastEl.textContent = message;
  toastEl.dataset.kind = kind;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2800);
}

async function copyText(text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // clipboard API needs https or localhost; fall back for LAN access
    const ta = h("textarea", { style: { position: "fixed", opacity: "0" } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } finally { ta.remove(); }
    if (!copied) throw new Error("Clipboard access failed. Select and copy the text manually.");
  }
  notify(`${what} to clipboard`);
}

function btn(label, fn, opts = {}) {
  const cls = ["pl-btn", opts.primary && "primary", opts.small && "small", opts.danger && "danger"];
  return h(
    "button",
    {
      class: cls.filter(Boolean).join(" "),
      type: "button",
      title: opts.title,
      onClick: (e) => {
        e.stopPropagation();
        Promise.resolve().then(() => fn(e)).catch((err) => { if (err.name !== "AbortError") notify(err.message || String(err), "error"); });
      },
    },
    label
  );
}

function bothText(p) {
  return p.negative ? `${p.positive}\n\nNegative prompt: ${p.negative}` : p.positive;
}

/* ------------------------------------------------------------------ */
/* Getting prompts into the workflow                                   */
/* ------------------------------------------------------------------ */

function textWidgets(node) {
  return (node.widgets || []).filter(
    (w) => typeof w.value === "string" && (w.type === "customtext" || w.type === "textarea" || w.options?.multiline)
  );
}

function pickTextWidget(node, role) {
  const ws = textWidgets(node);
  if (node.type === "PromptLibrary") return ws.find((w) => w.name === role) || ws[0];
  return ws[0];
}

function setWidgetValue(node, widget, value) {
  const graph = node.graph;
  graph?.beforeChange?.();
  try {
    widget.value = value;
    widget.callback?.(value, app.canvas, node, app.canvas?.graph_mouse, {});
  } finally {
    graph?.afterChange?.();
    graph?.setDirtyCanvas(true, true);
  }
}

function insertIntoSelected(text, role) {
  const nodes = Object.values(app.canvas?.selected_nodes || {});
  if (!nodes.length) return notify("Select a node with a text box first.", "warn");
  const choices = nodes.flatMap((node) => textWidgets(node).map((widget) => ({ node, widget })));
  if (choices.length === 1) {
    setWidgetValue(choices[0].node, choices[0].widget, text);
    return notify(`Inserted into "${choices[0].node.title}"`);
  }
  if (choices.length > 1) {
    let close;
    close = showOverlay(h("div", { class: "pl-editor" },
      h("strong", {}, `Choose a field for the ${role} prompt`),
      ...choices.map(({ node, widget }) => btn(`${node.title}: ${widget.name}`, () => {
        setWidgetValue(node, widget, text); close(); notify("Prompt inserted");
      })), btn("Cancel", () => close())), { className: "pl-narrow" });
    return;
  }
  notify("The selected node has no text box.", "warn");
}

let nodeOffset = 0;
function addTextNode(text, role) {
  const node = globalThis.LiteGraph?.createNode("CLIPTextEncode");
  if (!node) return notify("Could not find the CLIPTextEncode node.", "error");
  const va = app.canvas?.ds?.visible_area ?? app.canvas?.visible_area;
  nodeOffset = (nodeOffset + 30) % 150;
  node.pos = va
    ? [va[0] + va[2] / 2 - 200 + nodeOffset, va[1] + va[3] / 2 - 80 + nodeOffset]
    : [100 + nodeOffset, 100 + nodeOffset];
  app.graph.add(node);
  const w = pickTextWidget(node, "positive");
  if (w) setWidgetValue(node, w, text);
  if (role === "negative") node.title = "Negative prompt";
  app.graph.setDirtyCanvas(true, true);
  notify("Added a text node to the workflow");
}

function fillLibraryNode(node, p) {
  for (const role of ["positive", "negative"]) {
    const w = node.widgets?.find((x) => x.name === role);
    if (w) setWidgetValue(node, w, p[role] || "");
  }
  notify("Prompt loaded into the node");
}

/* ------------------------------------------------------------------ */
/* Scanning and saving                                                 */
/* ------------------------------------------------------------------ */

let scanning = null;
function runScan({ force = false } = {}) {
  const body = { force };
  if (scanning) {
    // The backend queues a follow-up request, including force requests.
    const current = scanning;
    return call("/scan", { method: "POST", body }).then(() => current);
  }
  scanning = (async () => {
    try {
      await call("/scan", { method: "POST", body });
      let st;
      do {
        await sleep(500);
        st = await call("/status");
        panels.forEach((p) => p.onScan?.(st));
      } while (st.running);
      return st;
    } finally {
      scanning = null;
    }
  })();
  return scanning;
}

function reloadAll() {
  for (const p of [...panels]) {
    if (!p.el.isConnected) { p.dispose(); panels.delete(p); }
    else p.reload();
  }
}

async function saveCurrentWorkflow() {
  const { output, workflow } = await app.graphToPrompt();
  const res = await call("/save_workflow", { method: "POST", body: { prompt: output, workflow } });
  if (!res.saved) return notify("No text prompts found in the current workflow.", "warn");
  notify(res.created ? "Saved the workflow's prompt to the library" : "Updated the existing library entry");
  reloadAll();
}

/* ------------------------------------------------------------------ */
/* Modals                                                              */
/* ------------------------------------------------------------------ */

document.addEventListener(
  "keydown",
  (e) => {
    if (e.key === "Escape" && overlays.length) {
      e.stopPropagation();
      overlays.pop().close();
    }
  },
  true
);

function showOverlay(content, { className = "", onClose } = {}) {
  let mouseDownOnBackdrop = false;
  const overlay = h(
    "div",
    {
      class: "pl-overlay",
      onMousedown: (e) => (mouseDownOnBackdrop = e.target === overlay),
      onClick: (e) => {
        if (e.target === overlay && mouseDownOnBackdrop) close();
      },
    },
    h("div", { class: `pl-modal ${className}` }, content)
  );
  const close = () => {
    const i = overlays.findIndex((o) => o.overlay === overlay);
    if (i >= 0) overlays.splice(i, 1);
    overlay.remove();
    onClose?.();
  };
  overlays.push({ overlay, close });
  document.body.append(overlay);
  return close;
}

function openLibrary({ targetNode = null } = {}) {
  let close;
  const panel = createPanel({ targetNode, onUse: () => close() });
  const header = h(
    "div",
    { class: "pl-modal-head" },
    h("strong", {}, targetNode ? "Choose a prompt for this node" : "Prompt library"),
    btn("Close", () => close(), { small: true })
  );
  panels.add(panel);
  close = showOverlay([header, panel.el], { className: "pl-library", onClose: () => { panel.dispose(); panels.delete(panel); } });
  panel.reload();
}

function openEditor(p, onSaved) {
  const title = h("input", { class: "pl-input", placeholder: "Optional title" });
  const positive = h("textarea", { class: "pl-input pl-area", rows: 6, placeholder: "Positive prompt" });
  const negative = h("textarea", { class: "pl-input pl-area", rows: 4, placeholder: "Negative prompt" });
  const tags = h("input", { class: "pl-input", placeholder: "portrait, moody, film grain" });
  const notes = h("textarea", { class: "pl-input pl-area", rows: 2, placeholder: "Notes (what it's good for, which model, ...)" });
  title.value = p?.title || "";
  positive.value = p?.positive || "";
  negative.value = p?.negative || "";
  tags.value = p?.tags || "";
  notes.value = p?.notes || "";

  const field = (label, el) => h("label", { class: "pl-field" }, h("span", { class: "pl-label" }, label), el);
  let close;
  const save = async () => {
    const body = {
      title: title.value,
      positive: positive.value,
      negative: negative.value,
      tags: tags.value,
      notes: notes.value,
    };
    if (p?.kind === "workflow") { delete body.positive; delete body.negative; }
    else if (!body.positive.trim() && !body.negative.trim()) return notify("Write a positive or negative prompt first.", "warn");
    const res = await call(p ? `/update/${p.id}` : "/save", { method: "POST", body });
    notify(p && res.id !== p.id ? "Saved an edited copy; original generation kept" : p ? "Changes saved" : "Prompt saved to the library");
    close();
    onSaved?.();
    reloadAll();
  };
  close = showOverlay(
    h(
      "div",
      { class: "pl-editor" },
      h("strong", {}, p ? "Edit prompt" : "New prompt"),
      field("Title", title),
      p?.kind === "workflow" ? null : field("Positive", positive),
      p?.kind === "workflow" ? null : field("Negative", negative),
      field("Tags (comma separated)", tags),
      field("Notes", notes),
      h("div", { class: "pl-row end" }, btn("Cancel", () => close()), btn(p ? "Save changes" : "Save prompt", save, { primary: true }))
    ),
    { className: "pl-narrow" }
  );
  (p ? positive : title).focus();
}

/* ------------------------------------------------------------------ */
/* The library panel                                                   */
/* ------------------------------------------------------------------ */

function createPanel({ targetNode = null, onUse = null } = {}) {
  const lifetime = new AbortController();
  const listRequests = latestRequest(lifetime.signal);
  const galleries = retainedResources();
  const selected = new Set();
  let collection = "";
  let selectionFilter = "";
  let collectionItems = [];
  const state = { search: "", filter: "all", sort: "latest", items: [], total: 0, open: new Set(), files: {}, scanText: "" };
  let seq = 0;
  let loading = false;
  let searchTimer;

  const searchEl = h("input", { class: "pl-input", type: "search", placeholder: "Search prompts, titles and tags" });
  const sortEl = h("select", { class: "pl-input pl-sort", title: "Sort order" }, SORTS.map(([v, l]) => h("option", { value: v }, l)));
  const collectionEl = h("select", { class: "pl-input", "aria-label": "Collection" });
  collectionEl.addEventListener("change", () => { collection = collectionEl.value; reload(); });
  const selectionBar = h("div", { class: "pl-row" });
  const bulkAction = h("select", { class: "pl-input", "aria-label": "Bulk action" },
    ...[["collection_add","Add to collection"],["collection_remove","Remove from collection"],["tag_add","Add tags"],["tag_remove","Remove tags"],["favorite","Favorite"],["unfavorite","Unfavorite"],["export","Export selected"],["remove","Remove selected"]].map(([value,label]) => h("option", { value }, label)));
  const bulkCollection = h("select", { class: "pl-input", "aria-label": "Target collection" });
  const bulkTags = h("input", { class: "pl-input", placeholder: "Tags, separated by commas", "aria-label": "Bulk tags" });
  const filters = () => ({ search: state.search, flt: state.filter, collection });
  async function applyBulk() {
    if (!selected.size) return notify("Select entries first.", "warn");
    const selection = { ids: [...selected] };
    let action = bulkAction.value;
    if (action === "export") return exportSelection(selection);
    let value = action.startsWith("collection_") ? bulkCollection.value : action.startsWith("tag_") ? bulkTags.value : action === "favorite";
    if (action === "unfavorite") action = "favorite";
    if (action === "remove" && !confirm(`Remove ${selected.size} selected entries from the library?`)) return;
    const result = await call("/bulk", { method: "POST", body: { selection, action, value } });
    selected.clear(); notify(`Updated ${result.count} entries`); reloadAll();
  }
  async function refreshCollections() {
    collectionItems = (await call("/collections", { signal: lifetime.signal })).items;
    if (lifetime.signal.aborted) return;
    if (collection && !collectionItems.some((item) => item.id === collection)) collection = "";
    collectionEl.replaceChildren(h("option", { value: "" }, "All collections"), ...collectionItems.map((item) => h("option", { value: item.id }, `${item.name} (${item.count})`)));
    collectionEl.value = collection;
    const previous = bulkCollection.value;
    bulkCollection.replaceChildren(...collectionItems.map((item) => h("option", { value: item.id }, item.name)));
    if (collectionItems.some((item) => item.id === previous)) bulkCollection.value = previous;
  }
  function renderSelection() {
    selectionBar.replaceChildren(h("span", { class: "pl-dim", "aria-live": "polite" }, `${selected.size} selected`),
      btn("Select page", () => { state.items.forEach((item) => selected.add(item.id)); render(); }),
      btn("Select all matching", async () => { const result = await call("/selection", { method: "POST", body: { filters: filters() } }); result.ids.forEach((id) => selected.add(id)); render(); }),
      btn("Clear selection", () => { selected.clear(); render(); }), bulkAction, bulkCollection, bulkTags, btn("Apply", applyBulk));
  }
  const chipsEl = h("div", { class: "pl-chips" });
  const listEl = h("div", { class: "pl-list" });
  const footEl = h("div", { class: "pl-foot" });
  const scanBtn = btn("Scan outputs", (e) => doScan(e.shiftKey), {
    primary: true,
    title: "Import prompts from your outputs folder. Shift-click to re-read every file.",
  });

  searchEl.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = searchEl.value.trim();
      reload();
    }, 250);
  });
  sortEl.addEventListener("change", () => {
    state.sort = sortEl.value;
    reload();
  });

  const el = h(
    "div",
    { class: "pl-root" },
    h("div", { class: "pl-row" }, searchEl, sortEl),
    h("div", { class: "pl-row" }, collectionEl, btn("Manage collections", () => manageCollections(reloadAll))),
    chipsEl,
    selectionBar,
    h(
      "div",
      { class: "pl-row" },
      scanBtn,
      btn("Save current workflow", saveCurrentWorkflow, { title: "Save the prompt used by the workflow open on the canvas" }),
      btn("New prompt", () => openEditor(null, reload)),
      btn("Scan report", showScanReport),
      btn("Export matching", () => exportSelection({ filters: filters() })),
      btn("Import archive", chooseImport),
      btn("Settings and backups", showLibrarySettings)
    ),
    listEl,
    footEl
  );

  async function load(offset = 0) {
    if (offset && loading) return;
    loading = true;
    const filterKey = JSON.stringify(filters());
    if (selectionFilter !== filterKey) { selected.clear(); selectionFilter = filterKey; }
    const mine = ++seq;
    const request = listRequests.next();
    const qs = new URLSearchParams({ search: state.search, filter: state.filter, sort: state.sort, collection, offset, limit: PAGE });
    let data;
    try { data = await call(`/list?${qs}`, { signal: request.signal }); } finally { request.finish(); if (mine === seq) loading = false; }
    if (mine !== seq || !request.current()) return; // a newer request replaced this one
    state.items = offset ? state.items.concat(data.items) : data.items;
    state.total = data.total;
    render();
  }
  const reload = () => {
    if (lifetime.signal.aborted) return Promise.resolve();
    listRequests.dispose();
    state.files = {};
    return refreshCollections().then(() => { if (!lifetime.signal.aborted) return load(0); })
      .catch((e) => { if (e.name !== 'AbortError') notify(e.message, "error"); });
  };

  async function doScan(force) {
    scanBtn.disabled = true;
    try {
      const st = await runScan({ force });
      if (st.error) notify(`Scan failed: ${st.error}`, "error");
      else notify(`${st.added || 0} new prompts. ${st.counts?.failed || 0} failed; ${st.counts?.deferred || 0} waiting. See Scan report.`);
      reloadAll();
    } finally {
      scanBtn.disabled = false;
    }
  }

  function render() {
    if (lifetime.signal.aborted) return;
    galleries.prune(state.items.filter((item) => state.open.has(item.id)).map((item) => item.id));
    renderSelection();
    chipsEl.replaceChildren(
      ...FILTERS.map(([value, label]) =>
        h(
          "button",
          {
            class: `pl-chip${state.filter === value ? " on" : ""}`,
            type: "button",
            onClick: () => {
              state.filter = value;
              reload();
            },
          },
          label
        )
      )
    );
    if (!state.items.length) {
      const filtered = state.search || state.filter !== "all";
      listEl.replaceChildren(
        h(
          "div",
          { class: "pl-empty" },
          filtered
            ? "No prompts match. Clear the search or pick another filter."
            : "Your library is empty. Choose Scan outputs to import prompts from your generated files, or Save current workflow to keep the prompt you're working on."
        )
      );
    } else {
      listEl.replaceChildren(...state.items.map(card));
    }
    renderFoot();
  }

  function renderFoot() {
    footEl.replaceChildren(
      h("span", { class: "pl-dim" }, state.scanText || `${state.total} prompt${state.total === 1 ? "" : "s"}`),
      state.items.length < state.total && !state.scanText
        ? btn("Load more", () => load(state.items.length), { small: true })
        : null
    );
  }

  function block(label, text, role) {
    return h(
      "div",
      { class: "pl-block" },
      h(
        "div",
        { class: "pl-block-head" },
        h("span", { class: "pl-label" }, label),
        h(
          "span",
          { class: "pl-btns" },
          btn("Copy", () => copyText(text, `${label} copied`), { small: true }),
          btn("Insert", () => insertIntoSelected(text, role), { small: true, title: "Put this text into the selected node" }),
          btn("Add node", () => addTextNode(text, role), { small: true, title: "Create a new text node with this prompt" })
        )
      ),
      h("div", { class: "pl-pre" }, text)
    );
  }

  function fileList(p, box) {
    const draw = (files) => {
      if (!files.length) {
        box.replaceChildren(h("div", { class: "pl-dim" }, "No output files are linked to this prompt."));
        return;
      }
      const gone = files.filter((f) => f.missing).length;
      box.replaceChildren(
        h("div", { class: "pl-dim" }, `${files.length} file${files.length === 1 ? "" : "s"}${gone ? `, ${gone} no longer in outputs` : ""}`),
        ...files.map((f) => {
          const i = f.path.lastIndexOf("/");
          const url = api.apiURL(
            `/view?${new URLSearchParams({ filename: f.path.slice(i + 1), subfolder: i < 0 ? "" : f.path.slice(0, i), type: "output" })}`
          );
          return f.missing || !f.current_root
            ? h("div", { class: "pl-file gone" }, `${f.path}${!f.current_root ? " (previous output folder)" : ""}`)
            : h("a", { class: "pl-file", href: url, target: "_blank", rel: "noopener" }, f.path);
        }),
        null
      );
    };
    if (state.files[p.id]) return draw(state.files[p.id]);
    box.textContent = "Loading files...";
    call(`/files/${p.id}`, { signal: lifetime.signal })
      .then((d) => {
        if (lifetime.signal.aborted) return;
        state.files[p.id] = d.files;
        draw(d.files);
      })
      .catch(() => (box.textContent = ""));
  }

  function metaChips(p) {
    const m = p.meta || {};
    const items = [
      m.model && String(m.model).replace(/\.(safetensors|ckpt|gguf|pt)$/i, ""),
      m.size,
      m.sampler && `${m.sampler}${m.scheduler ? " / " + m.scheduler : ""}`,
      m.steps != null && `${m.steps} steps`,
      m.cfg != null && `cfg ${m.cfg}`,
      m.seed != null && `seed ${m.seed}`,
      ...(m.loras || []).map((l) => `lora ${String(l).replace(/\.safetensors$/i, "")}`),
    ].filter(Boolean);
    return items.length ? h("div", { class: "pl-tags" }, items.map((t) => h("span", { class: "pl-tag" }, t))) : null;
  }

  function card(p) {
    const open = state.open.has(p.id);
    const firstLine = (p.positive || p.negative || "Workflow snapshot").split("\n")[0];
    const placeholder = () => h("div", { class: "pl-thumb empty" },
      p.media.includes("video") ? "Video" : p.media.includes("audio") ? "Audio" : "Text");
    const thumb = p.has_thumb
      ? h("img", { class: "pl-thumb", loading: "lazy", alt: "Prompt preview",
          src: `${api.apiURL(`${API}/thumb/${p.id}`)}?v=${encodeURIComponent(p.thumb_version || p.updated_at)}`,
          onError: (event) => event.currentTarget.replaceWith(placeholder()) })
      : placeholder();
    const tags = (p.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
    const badges = h(
      "div",
      { class: "pl-tags" },
      p.file_count ? h("span", { class: "pl-dim" }, `${p.available_count}/${p.file_count} files`) : null,
      tags.map((t) =>
        h(
          "button",
          {
            class: "pl-tag link",
            type: "button",
            onClick: (e) => {
              e.stopPropagation();
              searchEl.value = state.search = t;
              reload();
            },
          },
          t
        )
      )
    );

    const toggle = () => {
      open ? state.open.delete(p.id) : state.open.add(p.id);
      render();
    };
    const star = h(
      "button",
      {
        class: `pl-star${p.favorite ? " on" : ""}`,
        type: "button",
        title: p.favorite ? "Remove from favorites" : "Add to favorites",
        onClick: async (e) => {
          e.stopPropagation();
          try { await call(`/update/${p.id}`, { method: "POST", body: { favorite: !p.favorite } }); }
          catch (error) { notify(error.message, "error"); return; }
          if (state.filter === "favorites") reload();
          else {
            p.favorite = p.favorite ? 0 : 1;
            render();
          }
        },
      },
      p.favorite ? "\u2605" : "\u2606"
    );

    const quick = p.kind === "workflow" ? btn("Workflows", () => { state.open.add(p.id); render(); }, { small: true }) : targetNode
      ? btn("Use", () => { fillLibraryNode(targetNode, p); onUse?.(); }, { primary: true, small: true, title: "Fill the node with this prompt" })
      : btn("Copy", () => copyText(p.positive || p.negative, "Prompt copied"), { small: true, title: "Copy the positive prompt" });

    const head = h(
      "div",
      { class: "pl-head", onClick: toggle },
      h("input", { type: "checkbox", checked: selected.has(p.id), "aria-label": `Select ${p.title || firstLine || "workflow"}`,
        onClick: (event) => event.stopPropagation(), onChange: (event) => { event.target.checked ? selected.add(p.id) : selected.delete(p.id); renderSelection(); } }),
      thumb,
      h(
        "div",
        { class: "pl-main" },
        p.title || open ? h("div", { class: "pl-title" }, p.title || firstLine) : null,
        open ? null : h("div", { class: `pl-clamp${p.title ? " dim" : ""}` }, p.positive || p.negative),
        open ? null : badges
      ),
      h("div", { class: "pl-side" }, star, quick)
    );

    let body = null;
    if (open) {
      const filesBox = h("div", { class: "pl-files" });
      const resources = galleries.get(p.id, () => {
        const controller = new AbortController();
        const workflowsBox = h("div", { class: "pl-files" });
        const gallery = generationGallery(p.id);
        workflowList(p.id, workflowsBox, controller.signal).catch((error) => {
          if (error.name !== 'AbortError') workflowsBox.textContent = error.message;
        });
        return { workflowsBox, gallery, dispose() { controller.abort(); gallery.dispose(); } };
      });
      const workflowsBox = resources.workflowsBox;
      body = h(
        "div",
        { class: "pl-body" },
        p.positive ? block("Positive", p.positive, "positive") : null,
        p.negative ? block("Negative", p.negative, "negative") : null,
        ...(p.extra || []).map((x) => block(x.label || (x.role === "other" ? "Other text" : `Another ${x.role}`), x.text, x.role === "negative" ? "negative" : "positive")),
        ...(p.warnings || []).map((warning) => h("div", { class: "pl-notes" }, `Review: ${warning}`)),
        ...(p.stages || []).slice(1).map((stage, index) => h("div", { class: "pl-row" },
          h("span", { class: "pl-dim" }, `Stage ${index + 2}`),
          btn("Copy pair", () => copyText(bothText(stage))),
          targetNode ? btn("Use pair", () => { fillLibraryNode(targetNode, stage); onUse?.(); }) : null)),
        p.notes ? h("div", { class: "pl-notes" }, p.notes) : null,
        metaChips(p),
        badges,
        filesBox,
        workflowsBox,
        resources.gallery,
        h(
          "div",
          { class: "pl-row" },
          targetNode ? btn("Use in node", () => { fillLibraryNode(targetNode, p); onUse?.(); }, { primary: true }) : null,
          btn("Copy both", () => copyText(bothText(p), "Prompts copied"), { title: "Positive prompt followed by the negative prompt" }),
          btn("Edit", () => openEditor(p, reload)),

          btn(
            "Remove",
            async () => {
              if (!confirm("Remove this prompt from the library? Your output files are not touched.")) return;
              await call(`/delete/${p.id}`, { method: "POST" });
              state.items = state.items.filter((x) => x.id !== p.id);
              state.total--;
              render();
              notify("Removed from the library");
            },
            { danger: true }
          )
        )
      );
      fileList(p, filesBox);
    }
    return h("div", { class: `pl-card${open ? " open" : ""}` }, head, body);
  }

  return {
    el,
    reload,
    dispose() {
      lifetime.abort(); listRequests.dispose(); galleries.dispose(); clearTimeout(searchTimer);
    },
    onScan(st) {
      state.scanText = st.running ? `Scanning ${st.done} of ${st.total} new or changed files...` : "";
      renderFoot();
    },
  };
}

async function showScanReport() {
  const { scan, files } = await call("/report");
  let close;
  const counts = Object.entries(scan.counts || {}).map(([name, count]) => `${name.replaceAll("_", " ")}: ${count}`).join(" · ");
  close = showOverlay(h("div", { class: "pl-editor" },
    h("strong", {}, "Output scan report"), h("div", { class: "pl-pre" }, scan.root || "No scan yet"),
    h("div", {}, counts),
    scan.error ? h("div", { class: "pl-notes" }, scan.error) : null,
    h("div", { class: "pl-dim" }, "Files without recoverable metadata stay listed here. Deferred and temporary read failures are retried automatically."),
    ...(scan.issues || []).map((issue) => h("div", { class: "pl-pre" }, `${issue.path}: ${issue.reason}`)),
    h("details", {}, h("summary", {}, `All ${files.length} media files`),
      ...files.map((file) => h("div", { class: "pl-pre" }, `${file.path}: ${file.status}${file.missing ? " (missing)" : ""}${file.error ? " — " + file.error : ""}`))),
    btn("Close", () => close())), { className: "pl-narrow" });
}

function downloadLink(path, label) {
  return h("a", { class: "pl-btn small", href: api.apiURL(`${API}${path}`), download: "" }, label);
}

async function awaitJob(id, label) {
  const status = h("div", { class: "pl-pre", role: "status", "aria-live": "polite" }, "Queued…");
  let close;
  close = showOverlay(h("div", { class: "pl-editor" }, h("strong", {}, label), status,
    h("div", { class: "pl-dim" }, "This job continues if you close this window."), btn("Close", () => close())), { className: "pl-narrow" });
  try {
    while (true) {
      const job = await call(`/jobs/${id}`);
      status.textContent = job.state === "running" ? "Working…" : job.state;
      if (job.state === "failed") throw new Error(job.error || "Job failed");
      if (job.state === "complete") { close(); return job.result; }
      await sleep(500);
    }
  } catch (error) { status.textContent = error.message; throw error; }
}

async function exportSelection(selection) {
  const { job } = await call("/export", { method: "POST", body: selection });
  const result = await awaitJob(job, "Export library");
  let close;
  close = showOverlay(h("div", { class: "pl-editor" }, h("strong", {}, `${result.count} entries exported`),
    downloadLink(`/jobs/${job}/download`, "Download archive"), btn("Close", () => close())), { className: "pl-narrow" });
}

function chooseImport() {
  const input = h("input", { type: "file", accept: ".zip" });
  input.addEventListener("change", async () => {
    if (!input.files?.[0]) return;
    try {
      const response = await api.fetchApi(`${API}/import/preview`, { method: "POST", body: input.files[0], headers: { "Content-Type": "application/zip" } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Upload failed");
      const report = await awaitJob(data.job, "Check archive");
      let close;
      close = showOverlay(h("div", { class: "pl-editor" }, h("strong", {}, "Import preview"),
        h("div", {}, `${report.new} new · ${report.duplicates} existing · ${report.conflicts.length} annotation conflicts`),
        h("div", { class: "pl-dim" }, "Local edits and removals are preserved. Missing information, tags, collections and distinct history are merged."),
        report.conflicts_truncated ? h("div", {}, "Showing a shortened conflict preview. Download the full report for all details.") : null,
        downloadLink(`/jobs/${data.job}/download`, "Download full preview report"),
        ...report.conflicts.map((conflict) => h("details", {}, h("summary", {}, `Conflict: ${conflict.field}`),
          h("div", { class: "pl-pre" }, `Local: ${conflict.local}\nImported: ${conflict.imported}`))),
        btn("Import archive", async () => {
          close();
          const result = await call("/import/commit", { method: "POST", body: { preview_job: data.job } });
          await awaitJob(result.job, "Import library"); reloadAll();
          let finish;
          finish = showOverlay(h("div", { class: "pl-editor" }, h("strong", {}, "Import complete"),
            downloadLink(`/jobs/${result.job}/download`, "Download import report"), btn("Close", () => finish())), { className: "pl-narrow" });
        }, { primary: true }), btn("Cancel", () => close())), { className: "pl-narrow" });
    } catch (error) { notify(error.message, "error"); }
  });
  input.click();
}

async function manageCollections(onChange) {
  const content = h("div", { class: "pl-editor" });
  let close;
  async function draw() {
    const { items } = await call("/collections");
    content.replaceChildren(h("strong", {}, "Collections"), ...items.map((collection) => h("div", { class: "pl-row" },
      h("span", {}, `${collection.name} (${collection.count})`), btn("Rename", async () => {
        const name = prompt("Collection name", collection.name); if (!name) return;
        await call("/collections", { method: "POST", body: { id: collection.id, name } }); await draw(); onChange();
      }), btn("Delete collection", async () => {
        if (!confirm(`Delete collection “${collection.name}”? Its prompts will remain in the library.`)) return;
        await call(`/collections/${collection.id}/delete`, { method: "POST", body: {} }); await draw(); onChange();
      }))), btn("New collection", async () => {
        const name = prompt("Collection name"); if (!name) return;
        await call("/collections", { method: "POST", body: { name } }); await draw(); onChange();
      }), btn("Close", () => close()));
  }
  close = showOverlay(content, { className: "pl-narrow" });
  await draw();
}

function canOpenWorkflowTab() {
  return typeof app.extensionManager?.workflow?.createNewTemporary === "function" && typeof app.loadGraphData === "function";
}

async function openWorkflowTab(id) {
  if (!canOpenWorkflowTab()) throw new Error("This frontend cannot open a separate workflow tab. Download the workflow JSON instead.");
  const data = await call(`/workflow/${id}/editor`);
  const clone = JSON.parse(JSON.stringify(data));
  clone.id = globalThis.crypto?.randomUUID?.() || "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (letter) => {
    const value = Math.floor(Math.random() * 16);
    return (letter === "x" ? value : (value & 3) | 8).toString(16);
  });
  const name = `Library-${id.slice(0, 8)}-${Date.now()}.json`;
  const workflow = app.extensionManager.workflow.createNewTemporary(name, clone);
  const result = await app.loadGraphData(clone, true, true, workflow);
  if (result === false) throw new Error("The workflow could not be opened. Its JSON is still available to download.");
}

function workflowActions(workflow) {
  return h("div", { class: "pl-row" },
    workflow.has_editor ? downloadLink(`/workflow/${workflow.id}/editor?download=1`, "Download workflow") : null,
    workflow.has_editor && canOpenWorkflowTab() ? btn("Open workflow in new tab", () => openWorkflowTab(workflow.id)) : null,
    workflow.has_api ? downloadLink(`/workflow/${workflow.id}/api?download=1`, "Download API graph") : null);
}

async function workflowList(pid, container, signal) {
  const { items } = await call(`/workflows/${pid}`, { signal });
  if (signal?.aborted) return;
  container.replaceChildren(...items.map((workflow, index) => h("div", {},
    h("span", { class: "pl-dim" }, `Workflow ${index + 1}${workflow.has_editor ? "" : " (API only)"}`), workflowActions(workflow))));
}

function generationGallery(pid) {
  const controller = new AbortController();
  const container = h("div", { class: "pl-gallery" });
  container.dispose = () => controller.abort();
  let offset = 0;
  let busy = false;
  const more = btn("Load more generations", load);
  const cards = h("div", { class: "pl-gallery-grid" });
  container.append(cards, more);
  async function load() {
    if (busy || controller.signal.aborted) return;
    busy = true; more.disabled = true;
    try {
      const { items, total } = await call(`/gallery/${pid}?offset=${offset}&limit=12`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      for (const generation of items) {
        const mediaBox = h("div", { class: "pl-generation-preview" });
        if (generation.has_preview) mediaBox.append(h("img", { loading: "lazy", alt: "Generation preview", src: api.apiURL(`${API}/preview/${generation.id}?v=${generation.preview_version}`) }));
        else mediaBox.append(h("span", { class: "pl-dim" }, generation.media_type === "audio" ? "Audio" : "Preview unavailable"));
        const open = generation.available ? btn(generation.media_type === "image" ? "View image" : "Play media", () => {
          const tag = generation.media_type === "image" ? "img" : generation.media_type === "audio" ? "audio" : "video";
          let close;
          const media = h(tag, { class: "pl-player", src: api.apiURL(`${API}/media/${generation.id}`), controls: tag !== "img", preload: "metadata",
            onError: () => notify("This original is no longer available or its format cannot be played by this browser.", "warn") });
          close = showOverlay(h("div", { class: "pl-editor" }, media, btn("Close", () => close())),
            { className: "pl-library", onClose: () => { releaseMedia(media); } });
        }) : h("span", { class: "pl-dim" }, "Original missing, changed, or not linked locally");
        const snapshot = generation.snapshot || {};
        cards.append(h("div", { class: "pl-generation" }, mediaBox,
          h("div", { class: "pl-file" }, generation.path), h("div", { class: "pl-dim" }, new Date(generation.mtime * 1000).toLocaleString()), open,
          h("details", {}, h("summary", {}, "Prompts and settings"),
            h("div", { class: "pl-pre" }, bothText(snapshot)),
            ...(snapshot.extra || []).map((extra) => h("div", { class: "pl-pre" }, `${extra.label || extra.role}: ${extra.text}`)),
            h("div", { class: "pl-pre" }, JSON.stringify(snapshot.stages?.length ? snapshot.stages : snapshot.meta || {}, null, 2))),
          ...(generation.workflows || []).map(workflowActions)));
      }
      offset += items.length;
      more.hidden = offset >= total;
      if (!total) cards.append(h("div", { class: "pl-dim" }, "No source generations are linked to this entry."));
    } finally { busy = false; more.disabled = false; }
  }
  load().catch((error) => { if (error.name !== "AbortError") cards.append(h("div", {}, error.message)); });
  return container;
}

async function showLibrarySettings() {
  const settings = await call("/settings");
  const service = await call("/service");
  const fields = {};
  const labels = { capture_enabled: "Capture outputs while ComfyUI runs", scan_interval: "Scan interval (seconds)",
    include_hidden: "Include hidden output folders", follow_symlinks: "Follow linked files and folders", allowed_external_roots: "Allowed external folders (one absolute path per line)", backup_enabled: "Automatic backups",
    backup_interval_hours: "Backup interval (hours)", backup_keep: "Scheduled backups to keep", archive_max_mb: "Maximum archive upload (MB)",
    archive_expanded_max_mb: "Maximum expanded archive (MB)" };
  const form = Object.entries(labels).map(([key, label]) => {
    const boolean = typeof settings[key] === "boolean";
    const input = h(key === "allowed_external_roots" ? "textarea" : "input", { class: boolean ? "" : "pl-input", type: boolean ? "checkbox" : "number" });
    if (boolean) input.checked = settings[key]; else input.value = key === "allowed_external_roots" ? (settings[key] || []).join("\n") : settings[key];
    fields[key] = input;
    return h("label", { class: "pl-row" }, input, h("span", {}, label));
  });
  const date = (value) => value ? new Date(value * 1000).toLocaleString() : "Not scheduled";
  let close;
  const content = h("div", { class: "pl-editor" }, h("strong", {}, "Library settings and backups"), ...form,
    h("div", { class: "pl-dim" }, `Capture service: ${service.running ? "running" : "stopped"} · Next scan: ${date(service.next_scan)} · Next backup: ${date(service.next_backup)}`),
    service.last_error ? h("div", { class: "pl-notes" }, service.last_error) : null,
    btn("Save settings", async () => {
      const body = Object.fromEntries(Object.keys(labels).map((key) => [key, key === "allowed_external_roots" ? fields[key].value.split("\n").map((v) => v.trim()).filter(Boolean) : typeof settings[key] === "boolean" ? fields[key].checked : Number(fields[key].value)]));
      await call("/settings", { method: "POST", body }); notify("Library settings saved"); close();
    }, { primary: true }),
    btn("Back up now", async () => { const { job } = await call("/backups", { method: "POST", body: {} }); await awaitJob(job, "Create backup"); close(); await showLibrarySettings(); }),
    h("strong", {}, "Backups"),
    ...service.backups.map((backup) => h("div", { class: "pl-row" }, h("span", {}, `${date(backup.created_at)} (${backup.scheduled ? "scheduled" : "manual / safety"})`),
      downloadLink(`/backups/${backup.id}/download`, "Download"), btn("Restore", async () => {
        if (!confirm("Replace the library with this backup? A safety backup of the current library will be created first. Output files are not changed.")) return;
        const { job } = await call(`/backups/${backup.id}/restore`, { method: "POST", body: { confirm: true } });
        await awaitJob(job, "Restore library"); reloadAll(); close(); notify("Library restored; safety backup retained");
      }))),
    h("strong", {}, "Recent jobs"),
    ...(service.jobs || []).map((job) => h("div", { class: "pl-dim" }, `${job.kind}: ${job.state}${job.error ? " — " + job.error : ""}`)),
    btn("Close", () => close()));
  close = showOverlay(content, { className: "pl-narrow" });
}

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const CSS = `
.pl-gallery-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
.pl-generation{border:1px solid var(--pl-line);border-radius:6px;padding:8px;min-width:0;display:flex;flex-direction:column;gap:6px}
.pl-generation-preview{height:150px;display:flex;align-items:center;justify-content:center;background:var(--pl-panel)}
.pl-generation-preview img{max-width:100%;max-height:150px;object-fit:contain}
.pl-player{max-width:100%;max-height:65vh;object-fit:contain}
.pl-root> .pl-row{flex-shrink:0}

.pl-root,.pl-modal{--pl-fg:var(--fg-color,#ddd);--pl-dim:var(--descrip-text,#9b9b9b);--pl-line:var(--border-color,#4b4b4b);
  --pl-field:var(--comfy-input-bg,#2b2b2b);--pl-panel:var(--comfy-menu-bg,#1f1f1f);--pl-accent:var(--p-primary-color,#4f8fe8);
  color:var(--pl-fg);font-size:13px;line-height:1.45}
.pl-root *,.pl-modal *{box-sizing:border-box}
.pl-root{display:flex;flex-direction:column;gap:8px;height:100%;min-height:0;padding:10px}
.pl-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pl-row.end{justify-content:flex-end}
.pl-input{flex:1;min-width:0;background:var(--pl-field);color:var(--pl-fg);border:1px solid var(--pl-line);border-radius:5px;padding:6px 8px;font:inherit}
.pl-input:focus-visible,.pl-btn:focus-visible,.pl-chip:focus-visible,.pl-head:focus-visible{outline:2px solid var(--pl-accent);outline-offset:1px}
.pl-sort{flex:none}
.pl-area{resize:vertical}
.pl-chips{display:flex;flex-wrap:wrap;gap:4px}
.pl-chip{background:transparent;color:var(--pl-dim);border:1px solid var(--pl-line);border-radius:999px;padding:2px 10px;font:inherit;cursor:pointer}
.pl-chip.on{background:var(--pl-accent);border-color:var(--pl-accent);color:#fff}
.pl-btn{background:var(--pl-field);color:var(--pl-fg);border:1px solid var(--pl-line);border-radius:5px;padding:5px 10px;font:inherit;cursor:pointer;white-space:nowrap}
.pl-btn:hover{border-color:var(--pl-accent)}
.pl-btn:disabled{opacity:.5;cursor:default}
.pl-btn.primary{background:var(--pl-accent);border-color:var(--pl-accent);color:#fff}
.pl-btn.small{padding:2px 8px;font-size:12px}
.pl-btn.danger:hover{border-color:#d9534f;color:#ef7b77}
.pl-list{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:6px;padding-right:2px}
.pl-empty{padding:24px 12px;text-align:center;color:var(--pl-dim)}
.pl-card{border:1px solid var(--pl-line);border-radius:6px;background:var(--pl-field);flex:none}
.pl-card.open{border-color:var(--pl-accent)}
.pl-head{display:flex;gap:10px;padding:8px;cursor:pointer}
.pl-thumb{width:56px;height:56px;border-radius:4px;object-fit:cover;flex:none;background:var(--pl-panel)}
.pl-thumb.empty{display:flex;align-items:center;justify-content:center;color:var(--pl-dim);font-size:11px;border:1px dashed var(--pl-line)}
.pl-main{flex:1;min-width:0}
.pl-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pl-clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.pl-clamp.dim{-webkit-line-clamp:2;color:var(--pl-dim)}
.pl-side{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex:none}
.pl-star{background:none;border:0;color:var(--pl-dim);font-size:17px;line-height:1;cursor:pointer;padding:0 2px}
.pl-star.on{color:#e8b43c}
.pl-tags{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin-top:3px}
.pl-tag{border:1px solid var(--pl-line);border-radius:4px;padding:0 6px;font-size:12px;color:var(--pl-dim);background:transparent;font-family:inherit}
.pl-tag.link{cursor:pointer}
.pl-tag.link:hover{border-color:var(--pl-accent);color:var(--pl-fg)}
.pl-dim{color:var(--pl-dim);font-size:12px}
.pl-body{border-top:1px solid var(--pl-line);padding:8px;display:flex;flex-direction:column;gap:10px}
.pl-block-head{display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:3px;flex-wrap:wrap}
.pl-btns{display:flex;gap:4px}
.pl-label{font-weight:600}
.pl-pre{white-space:pre-wrap;word-break:break-word;background:var(--pl-panel);border:1px solid var(--pl-line);border-radius:4px;padding:6px 8px;max-height:220px;overflow:auto;user-select:text;cursor:text}
.pl-notes{color:var(--pl-dim);white-space:pre-wrap}
.pl-files{display:flex;flex-direction:column;gap:1px}
.pl-file{font-size:12px;color:var(--pl-accent);word-break:break-all;text-decoration:none}
.pl-file:hover{text-decoration:underline}
.pl-file.gone{color:var(--pl-dim);text-decoration:line-through}
.pl-foot{display:flex;justify-content:space-between;align-items:center;min-height:24px}
.pl-overlay{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
.pl-modal{background:var(--pl-panel);border:1px solid var(--pl-line);border-radius:8px;display:flex;flex-direction:column;max-width:94vw;max-height:90vh;overflow:hidden}
.pl-modal.pl-library{width:min(880px,94vw);height:min(780px,88vh)}
.pl-modal.pl-narrow{width:min(560px,94vw);overflow:auto}
.pl-modal-head{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-bottom:1px solid var(--pl-line)}
.pl-modal.pl-library .pl-root{flex:1}
.pl-editor{display:flex;flex-direction:column;gap:10px;padding:14px}
.pl-field{display:flex;flex-direction:column;gap:3px}
.pl-toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,12px);z-index:10001;background:var(--comfy-menu-bg,#222);color:var(--fg-color,#eee);border:1px solid var(--border-color,#555);border-left:4px solid #4f8fe8;border-radius:6px;padding:8px 14px;opacity:0;pointer-events:none;transition:opacity .15s,transform .15s;font-size:13px}
.pl-toast[data-kind=warn]{border-left-color:#e8b43c}
.pl-toast[data-kind=error]{border-left-color:#d9534f}
.pl-toast.show{opacity:1;transform:translate(-50%,0)}
.pl-fab{position:fixed;left:12px;bottom:12px;z-index:900}
@media (prefers-reduced-motion:reduce){.pl-toast{transition:none}}
`;
document.head.append(h("style", {}, CSS));

/* ------------------------------------------------------------------ */
/* Extension registration                                              */
/* ------------------------------------------------------------------ */

app.registerExtension({
  name: "PromptLibrary",

  commands: [
    {
      id: "PromptLibrary.Open",
      label: "Open Prompt Library",
      icon: "pi pi-book",
      function: () => openLibrary(),
    },
    {
      id: "PromptLibrary.SaveWorkflow",
      label: "Save current workflow's prompt to Prompt Library",
      icon: "pi pi-bookmark",
      function: () => saveCurrentWorkflow().catch((e) => notify(e.message, "error")),
    },
  ],

  setup() {
    if (app.extensionManager?.registerSidebarTab) {
      app.extensionManager.registerSidebarTab({
        id: "prompt-library",
        icon: "pi pi-book",
        title: "Prompts",
        tooltip: "Prompt Library",
        type: "custom",
        render: (container) => {
          container.style.height = "100%";
          const panel = createPanel();
          panels.add(panel);
          container.append(panel.el);
          panel.reload();
        },
      });
    } else {
      // Older frontends without sidebar tabs: a small floating button instead.
      document.body.append(h("div", { class: "pl-fab" }, btn("Prompt library", () => openLibrary())));
    }

    // Also observe startup scans and backend retries, which can finish without a browser request.
    let polling = false;
    let lastFinished = null;
    setInterval(async () => {
      for (const panel of [...panels]) if (!panel.el.isConnected) { panel.dispose(); panels.delete(panel); }
      if (!panels.size || polling) return;
      polling = true;
      try {
        const st = await call("/status");
        panels.forEach((panel) => panel.onScan?.(st));
        if (!st.running && st.finished !== lastFinished) {
          lastFinished = st.finished;
          reloadAll();
        }
      } catch (error) { console.warn("[Prompt Library] status unavailable", error); }
      finally { polling = false; }
    }, 2000);
  },

  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "PromptLibrary") return;
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      const open = this.addWidget("button", "Open library", null, () => openLibrary({ targetNode: this }));
      const save = this.addWidget("button", "Save to library", null, async () => {
        const value = (name) => this.widgets.find((w) => w.name === name)?.value ?? "";
        const positive = value("positive");
        const negative = value("negative");
        if (!positive.trim() && !negative.trim()) return notify("Write a prompt in the node first.", "warn");
        try {
          const res = await call("/save", { method: "POST", body: { positive, negative } });
          notify(res.created ? "Saved to the library" : "Updated the existing library entry");
          reloadAll();
        } catch (e) {
          notify(e.message, "error");
        }
      });
      open.serialize = false;
      save.serialize = false;
      const size = this.computeSize();
      this.setSize([Math.max(size[0], 360), size[1]]);
      return result;
    };
  },
});
