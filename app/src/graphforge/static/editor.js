/* GraphForge editor.
 *
 * Weight and arrowhead carry what the graph means; colour is the user's.
 * No framework: HTMX handles the forms, this file owns the canvas.
 */
"use strict";

const CFG = JSON.parse(document.getElementById("gf-config").textContent);
const CSRF = document.cookie.match(/(?:^|;\s*)gf_csrf=([^;]*)/)?.[1] ?? "";

const C = {
  ground: "#10141c",
  panel: "#171d28",
  hairline: "#2a3341",
  paper: "#e9e6df",
  paperDim: "#a0a8b6",
  pencil: "#7a8aa3",
  firm: "#3987e5",
  inked: "#d95926",
};

/* Ingest stages, in the user's words rather than the pipeline's. Declared up
   here with the other constants: a `const` is not hoisted, and this is read by
   code that runs during script parse. */
const STAGE_WORDS = {
  pending: "Queued",
  parsing: "Reading the document",
  extracting: "Finding concepts and how they connect",
  resolving: "Merging concepts that mean the same thing",
  failed: "Could not read this document",
};

const SHAPES = CFG.shapes;
const SPARE_SHAPES = CFG.spareShapes || ["hexagon"];

/* Entity types are whatever the document called for -- "drug", "clause",
   "service" -- so a shape has to exist for types this file has never seen.
   Hashing the name keeps it stable: the same type is the same shape on every
   load, which is what makes shape readable as a category at all. */
function shapeFor(type) {
  const key = String(type || "concept").toLowerCase();
  if (SHAPES[key]) return SHAPES[key];
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  return SPARE_SHAPES[hash % SPARE_SHAPES.length];
}

/* An element's state is the only thing colour encodes. */
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      "X-CSRF-Token": CSRF,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed (${res.status})`);
  }
  return res.status === 204 ? null : res.json();
}

/* ----------------------------------------------------------------- canvas */
const cy = cytoscape({
  container: document.getElementById("canvas"),
  wheelSensitivity: 0.25,
  minZoom: 0.1,
  maxZoom: 4,
  // Dragging the background pans, which is what a canvas this size needs far
  // more often than it needs a selection box. Box selection lives on
  // shift-drag, where it costs plain dragging nothing.
  boxSelectionEnabled: true,
  style: [
    {
      selector: "node",
      style: {
        shape: (n) => shapeFor(n.data("type")),
        // Diameter carries how central the concept is -- see sizeFor in
        // layout.js. Uniform dots gave the reader no way in.
        width: (n) => sizeFor(n),
        height: (n) => sizeFor(n),
        "background-color": C.panel,
        "border-width": 2,
        // A stored colour wins. The click-time inline style made a new colour
        // look applied, but on reload the stylesheet ran again and this
        // function handed back the default -- so the colour appeared to vanish
        // when it had been saved all along.
        "border-color": (n) =>
          n.data("color") || (n.data("user_edited") ? C.inked : C.pencil),
        label: "data(name)",
        // Bright enough to read against the ink ground. The previous dim grey
        // was chosen to keep labels quiet, but quiet is useless if unreadable.
        color: C.paper,
        "font-family": "Plex Sans, system-ui, sans-serif",
        "font-size": (n) => labelSizeFor(n),
        "text-valign": "bottom",
        // Follows the node, so a big node's label does not sit in its lap.
        "text-margin-y": (n) => 4 + sizeFor(n) / 4,
        "text-max-width": 96,
        // Wrap rather than ellipsis: a truncated concept name is not a name.
        "text-wrap": "wrap",
        // A slab of the ground colour behind each label, so two that overlap
        // are still individually legible instead of becoming a smear.
        "text-background-color": C.ground,
        "text-background-opacity": 0.82,
        "text-background-padding": 3,
        "text-background-shape": "roundrectangle",
        "text-events": "yes",
        "min-zoomed-font-size": 7,
      },
    },
    {
      selector: "node:selected",
      style: {
        "border-color": C.inked,
        "border-width": 3,
        color: C.paper,
        "font-weight": 600,
      },
    },
    {
      /* The selection marquee, in the interface's own ink rather than the
         library's default blue. */
      selector: "core",
      style: {
        "selection-box-color": C.inked,
        "selection-box-opacity": 0.1,
        "selection-box-border-color": C.inked,
        "selection-box-border-width": 1,
        "active-bg-opacity": 0,
        "outside-texture-bg-color": C.ground,
      },
    },
    { selector: "node.dim", style: { opacity: 0.18 } },
    /* Nodes (and therefore their labels) paint above edges, so a line never
       runs through a name. */
    { selector: "node", style: { "z-index": 10 } },
    { selector: "edge", style: { "z-index": 1 } },
    /* Not from the selected document: removed from the view entirely rather
       than dimmed, so "showing one document" means exactly that. */
    { selector: ".filtered", style: { display: "none" } },
    {
      selector: "node.neighbour",
      style: { "border-color": C.firm, color: C.paper },
    },
    {
      /* One line style for every stated link.
       *
       * These used to be dashed below a confidence of 0.70, solid above it,
       * and orange once a human had confirmed them -- a visual language built
       * when extraction was a local encoder that guessed wrong often enough
       * for "how sure is this?" to be the first thing you needed to know.
       * Reading a whole document with a model that scores 0.90 changes the
       * question, and a canvas that codes an answered question is just noise.
       * Colour is the user's now, not the model's.
       */
      selector: "edge",
      style: {
        width: 1.6,
        "line-color": C.firm,
        "line-style": "solid",
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "target-arrow-color": C.firm,
        // Direction is half the meaning of a relationship: "A uses B" and
        // "B uses A" are different claims.
        "arrow-scale": 1.3,
        "target-distance-from-node": 3,
        "source-distance-from-node": 2,
        opacity: 0.85,
      },
    },
    {
      /* A prerequisite is not a claim about the subject, it is an instruction
         to the reader -- "this one first" -- so it is drawn as a route rather
         than as a relationship: same ink as a confirmed edge, but stepped. */
      /* A prerequisite is told apart by its head and its weight, not by its
         colour.
         *
         * Colour is the user's -- they can paint any edge any colour -- so the
         * moment the graph uses colour to mean something, colouring an edge
         * erases that meaning. Shape and weight are channels nothing else
         * writes to, so a recoloured prerequisite is still obviously a
         * prerequisite. The ink below is only its default. */
      selector: "edge[?prerequisite]",
      style: {
        width: 3,
        "line-color": C.inked,
        "target-arrow-color": C.inked,
        // A barred head: the arrow stops at a wall, "get here first".
        "target-arrow-shape": "triangle-tee",
        "arrow-scale": 1.5,
        opacity: 1,
      },
    },
    {
      /* The concepts the document is actually about. */
      selector: 'node[importance = "core"]',
      style: { "border-width": 3, "background-color": C.ground, "font-weight": 600 },
    },
    {
      /* An explicit colour outranks the hues above it -- and nothing else.
         Width and arrow shape are deliberately not set here, so recolouring an
         edge cannot flatten a prerequisite into an ordinary link. */
      selector: "edge[color]",
      style: { "line-color": "data(color)", "target-arrow-color": "data(color)" },
    },
    {
      selector: "edge:selected",
      style: { width: 3, "line-color": C.paper, "target-arrow-color": C.paper, opacity: 1 },
    },
    { selector: "edge.dim", style: { opacity: 0.06 } },
    /* The edge under the cursor brightens, so in a dense area it is obvious
       which line the tooltip is describing. */
    {
      selector: "edge.hovered",
      style: { "line-color": C.paper, "target-arrow-color": C.paper, opacity: 1, "z-index": 20 },
    },
    {
      selector: "edge.labelled",
      style: {
        label: "data(type)",
        "font-family": "Plex Mono, monospace",
        "font-size": 9,
        color: C.paper,
        "text-background-color": C.ground,
        "text-background-opacity": 0.92,
        "text-background-padding": 3,
        "text-rotation": "autorotate",
      },
    },
  ],
  layout: { name: "grid" },
});

let layoutName = "web";
try {
  const saved = localStorage.getItem("gf:layout");
  if (saved && LAYOUTS[saved]) layoutName = saved;
} catch (e) { /* storage unavailable */ }

function runLayout(name) {
  if (name) {
    layoutName = name;
    try { localStorage.setItem("gf:layout", name); } catch (e) { /* ignore */ }
  }
  for (const key of Object.keys(LAYOUTS)) {
    const btn = document.getElementById("layout-" + key);
    if (btn) btn.setAttribute("aria-pressed", String(key === layoutName));
  }
  // Lay out only what is on screen: a filtered-out component should not be
  // reserving space in the middle of the arrangement.
  const visible = cy.elements().not(".filtered");
  const eles = visible.length ? visible : cy.elements();
  // Never one layout over the whole graph -- see layout.js for why.
  arrange(cy, eles, LAYOUTS[layoutName]);
  // Degree drives node size, and filtering changes degree.
  cy.nodes().forEach((n) => n.style({
    width: sizeFor(n), height: sizeFor(n),
    "font-size": labelSizeFor(n), "text-margin-y": 4 + sizeFor(n) / 4,
  }));
}

/* ------------------------------------------------------------------ load */
/* One line on what the open document teaches. A map of five circles does not
   say what subject it is a map of; this does, and it is the first thing the
   model is asked for. */
function showTeaches(documents) {
  const bar = document.getElementById("canvas-teaches");
  if (!bar) return;
  const selected = document.querySelector('.doc[aria-pressed="true"]');
  const match = selected
    ? documents.find((d) => d.id === selected.dataset.id)
    : documents.length === 1
      ? documents[0]
      : null;
  bar.textContent = match && match.teaches ? match.teaches : "";
  bar.hidden = !bar.textContent;
}

/* Contrasts belong to the document, not to any one concept, so they are held
   here and the panel filters to the rows its concept appears in. */
let CONTRASTS = [];

async function loadGraph() {
  const data = await api(`/api/w/${CFG.workspaceId}/graph`);

  CONTRASTS = (data.documents || []).flatMap((d) => {
    try {
      return JSON.parse(d.contrasts || "[]");
    } catch (e) {
      return [];
    }
  });
  showTeaches(data.documents || []);

  cy.elements().remove();
  cy.add([
    ...data.nodes.map((n) => ({
      data: {
        id: n.id, name: n.name, type: n.type,
        one_liner: n.one_liner || "",
        importance: n.importance || "supporting",
        user_edited: !!n.user_edited,
        color: n.color || null,
        // Kept on the node so narrowing to one document is a local filter.
        documents: n.documents || [],
      },
    })),
    ...data.edges.map((e) => ({
      data: {
        id: `${e.source}~${e.type}~${e.target}`,
        source: e.source,
        target: e.target,
        type: e.type,
        why: e.why || "",
        prerequisite: !!e.prerequisite,
        // Without this the `edge[color]` rule matches nothing after a reload,
        // and a colour that was saved correctly looks like it was lost.
        color: e.color || null,
        user_edited: !!e.user_edited,
      },
    })),
  ]);

  // Before the layout, not after. Every element here is new, so the filter is
  // gone -- and laying out first would arrange the other documents' concepts
  // into the arrangement and then hide them, leaving their gaps behind.
  applyDocumentFilter();

  if (cy.nodes().not(".filtered").length) {
    runLayout();
  }
  refreshCanvasView();
  updateStatus();
}

/* Hiding via the `hidden` attribute alone was not enough: a class that sets
   `display` outranks it, so the panel stayed over a full canvas. An inline
   style wins over both, and does not depend on the stylesheet being fresh. */
function showEmptyState(show) {
  const el = document.querySelector(".canvas-empty");
  el.hidden = !show;
  el.style.display = show ? "grid" : "none";
}

/* An empty canvas and a failed load are different situations and need
   different words. Previously the error text was written into the first <p> in
   the block -- which is the eyebrow -- so a failure read "SOMETHING WENT
   WRONG." above an invitation to add a document, with no way to retry. */
function showEmptyCanvas() {
  document.getElementById("canvas-empty-title").textContent = "Empty canvas";
  document.getElementById("canvas-empty-body").textContent =
    "Add a document and the model will sketch a graph from it. You correct what it gets wrong.";
  document.getElementById("upload-btn-empty").hidden = false;
  document.getElementById("canvas-retry").hidden = true;
  showEmptyState(true);
}

function showLoadError(message) {
  document.getElementById("canvas-empty-title").textContent = "Could not load the graph";
  document.getElementById("canvas-empty-body").textContent =
    (message || "The graph database did not respond.") +
    " Your documents are safe — this is a connection problem, not lost work.";
  document.getElementById("upload-btn-empty").hidden = true;
  document.getElementById("canvas-retry").hidden = false;
  showEmptyState(true);
}

function updateStatus() {
  const edges = cy.edges().not(".filtered");
  document.getElementById("stat-nodes").textContent = cy.nodes().not(".filtered").length;
  document.getElementById("stat-edges").textContent = edges.length;
  document.getElementById("stat-prereqs").textContent =
    edges.filter("[?prerequisite]").length;
}

/* ------------------------------------------------------------ side panel */
/* One dock, two views. `panel` still names the node view's state holder so the
   rest of this file reads unchanged, but open/close now belongs to the dock. */
const dock = document.getElementById("dock");
const panel = document.getElementById("pane-node");

function showDock(view) {
  dock.dataset.open = "true";
  dock.dataset.view = view;
  document.getElementById("pane-node").hidden = view !== "node";
  document.getElementById("pane-chat").hidden = view !== "chat";
  document.getElementById("tab-node").setAttribute("aria-selected", String(view === "node"));
  document.getElementById("tab-chat").setAttribute("aria-selected", String(view === "chat"));
  setTimeout(() => cy.resize(), 210);
}

function closeDock() {
  dock.dataset.open = "false";
  clearFocus();
  setTimeout(() => cy.resize(), 210);
}

/* Undo the canvas focus applied when a node was opened. */
function clearFocus() {
  cy.elements().removeClass("dim neighbour");
  cy.edges().removeClass("labelled");
}

// Kept for callers that only mean "stop focusing a node".
function closePanel() {
  if (dock.dataset.view === "node") closeDock();
  else clearFocus();
}

document.getElementById("tab-node").addEventListener("click", () => showDock("node"));
document.getElementById("tab-chat").addEventListener("click", () => showDock("chat"));
document.getElementById("dock-close").addEventListener("click", closeDock);
document.getElementById("chat-toggle").addEventListener("click", () => showDock("chat"));

async function openNode(node) {
  const id = node.id();

  /* Focus: everything unrelated recedes so the neighbourhood reads clearly. */
  const near = node.closedNeighborhood();
  cy.elements().addClass("dim");
  near.removeClass("dim");
  near.nodes().not(node).addClass("neighbour");
  near.edges().addClass("labelled");

  showDock("node");
  document.getElementById("panel-title").textContent = node.data("name");
  document.getElementById("panel-type").textContent = node.data("type");
  document.getElementById("panel-sources").innerHTML =
    '<p class="source-none">Loading source…</p>';
  document.getElementById("panel-map").replaceChildren();
  document.getElementById("panel-lead").hidden = true;

  let detail;
  try {
    detail = await api(`/api/w/${CFG.workspaceId}/node/${encodeURIComponent(id)}`);
  } catch (err) {
    const box = document.getElementById("panel-sources");
    box.replaceChildren();
    const p = document.createElement("p");
    p.className = "source-none";
    p.textContent = err.message;
    box.appendChild(p);
    return;
  }

  panel.dataset.nodeId = id;
  paintRelations(id);
  // Says something only when there is something to say. Every concept used to
  // be labelled "machine draft", which was the confidence framing by another
  // name: with extraction that reads the whole document, "the machine made
  // this" is true of everything and worth saying about nothing.
  const chip = document.getElementById("panel-state");
  const edited = !!node.data("user_edited");
  chip.dataset.state = edited ? "inked" : "";
  chip.textContent = edited ? "edited by you" : "";
  chip.hidden = !edited;

  renderMap(detail, node.data("name"));

  const box = document.getElementById("panel-sources");
  if (!detail.sources?.length) {
    box.innerHTML =
      '<p class="source-none">No source passage recorded for this node.</p>';
    return;
  }

  /* Verbatim document text — never a summary. This is the grounding. */
  renderSources(box, detail.sources, node.data("name"));
}

/* What the document says about one concept.
 *
 * Everything here came out of the user's PDF by way of the model, so it is
 * built with textContent and never as an HTML string -- a crafted document
 * must not be able to put markup on this page. Same rule as renderSources.
 */
const FIELDS = [
  ["how_it_works", "How it works", true],
  ["strengths", "Good at", false],
  ["limitations", "Limits", false],
  ["when_to_use", "Use it when", false],
  ["examples", "For example", false],
];

function renderMap(detail, nodeName) {
  const lead = document.getElementById("panel-lead");
  lead.textContent = detail.one_liner || "";
  lead.hidden = !detail.one_liner;

  const box = document.getElementById("panel-map");
  box.replaceChildren();

  renderPrereqs(box, nodeName);

  for (const [key, label, ordered] of FIELDS) {
    const items = detail[key] || [];
    if (!items.length) continue;
    const section = document.createElement("div");
    section.className = ordered ? "field ordered" : "field";
    const heading = document.createElement("p");
    heading.className = "field-name";
    heading.textContent = label;
    section.appendChild(heading);
    const list = document.createElement(ordered ? "ol" : "ul");
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    }
    section.appendChild(list);
    box.appendChild(section);
  }

  renderContrasts(box, nodeName);
}

/* Prerequisites, read off the graph rather than the node payload: they are
   edges, so the canvas and this list can never disagree about them. */
function renderPrereqs(box, nodeName) {
  const node = cy.nodes().filter((n) => n.data("name") === nodeName);
  if (!node.length) return;
  const needs = node
    .outgoers("edge[?prerequisite]")
    .targets()
    .map((n) => ({ id: n.id(), name: n.data("name") }));
  if (!needs.length) return;

  const wrap = document.createElement("div");
  wrap.className = "prereq";
  const heading = document.createElement("p");
  heading.className = "field-name";
  heading.textContent = needs.length === 1 ? "Understand this first" : "Understand these first";
  wrap.appendChild(heading);

  const line = document.createElement("p");
  line.style.margin = "0";
  needs.forEach((need, i) => {
    if (i) line.appendChild(document.createTextNode(", "));
    const link = document.createElement("button");
    link.type = "button";
    link.textContent = need.name;
    // Following a prerequisite is the whole point of recording it.
    link.addEventListener("click", () => {
      const target = cy.getElementById(need.id);
      if (target.length) {
        cy.elements().unselect();
        target.select();
        openNode(target);
      }
    });
    line.appendChild(link);
  });
  wrap.appendChild(line);
  box.appendChild(wrap);
}

/* The dimensions the document compares this concept against others on.
 *
 * Held on the document rather than the node -- a contrast is a row about
 * several concepts -- so the panel filters to the rows this one appears in.
 */
function renderContrasts(box, nodeName) {
  const rows = CONTRASTS.filter((c) => nodeName in (c.values || {}));
  if (!rows.length) return;

  const heading = document.createElement("p");
  heading.className = "field-name";
  heading.textContent = "Compared with";
  box.appendChild(heading);

  for (const row of rows) {
    const wrap = document.createElement("div");
    wrap.className = "contrast";
    const caption = document.createElement("p");
    caption.className = "field-name";
    caption.textContent = row.dimension;
    wrap.appendChild(caption);

    const table = document.createElement("table");
    for (const [concept, value] of Object.entries(row.values)) {
      const tr = document.createElement("tr");
      if (concept === nodeName) tr.className = "is-self";
      const th = document.createElement("th");
      th.scope = "row";
      th.textContent = concept;
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(th, td);
      table.appendChild(tr);
    }
    wrap.appendChild(table);
    box.appendChild(wrap);
  }
}

/* Build the source passages as DOM nodes.
 *
 * Not innerHTML: this is text straight out of a user's PDF, and the moment it
 * becomes an HTML string a crafted document can put markup on the page. Every
 * piece of document text below goes through textContent.
 */
function renderSources(box, sources, nodeName) {
  box.replaceChildren();

  for (const s of sources) {
    const figure = document.createElement("figure");
    figure.className = "source";

    // Citation first: knowing where a passage is from changes how you read
    // it, so it belongs before the text rather than after.
    const cite = document.createElement("figcaption");
    cite.className = "source-cite";
    cite.textContent =
      s.document +
      (s.page ? "  ·  page " + s.page : "") +
      "  ·  chars " + s.char_start + "–" + s.char_end;
    figure.appendChild(cite);

    const text = s.text || "";
    const focus = sentenceAround(text, parseSpans(s.spans), nodeName);

    const body = document.createElement("p");
    body.className = "source-text";
    highlightInto(body, focus.text, nodeName);
    figure.appendChild(body);

    // The rest of the chunk is context, not the answer. It stays one click
    // away rather than burying the sentence that actually mentions the node.
    if (focus.trimmed) {
      const more = document.createElement("details");
      more.className = "source-more";
      const summary = document.createElement("summary");
      summary.textContent =
        "Show the full passage (" + text.length.toLocaleString() + " characters)";
      more.appendChild(summary);
      const full = document.createElement("p");
      full.className = "source-text";
      highlightInto(full, text, nodeName);
      more.appendChild(full);
      figure.appendChild(more);
    }

    box.appendChild(figure);
  }
}

/* "412-434" -> [412, 434]. Stored as one string so a start and its end cannot
   drift apart during deduplication. */
function parseSpans(raw) {
  return (raw || [])
    .map((v) => String(v).split("-").map(Number))
    .filter((p) => p.length === 2 && Number.isFinite(p[0]) && Number.isFinite(p[1]))
    .sort((a, b) => a[0] - b[0]);
}

/* Narrow a chunk down to the sentence the model actually found the node in.
 *
 * The extractor reports the character range of every entity it spots. Those
 * offsets used to be thrown away, so a node could only ever show its whole
 * chunk — several thousand characters, starting and ending wherever the size
 * cap happened to fall. With the offsets kept, the panel can open on the
 * sentence and leave the rest as context.
 *
 * Falls back to a plain search when there are no offsets, which is the case
 * for documents ingested before they were stored.
 */
function sentenceAround(text, spans, nodeName) {
  let start = null;
  let end = null;

  if (spans.length) {
    start = spans[0][0];
    end = spans[0][1];
  } else if (nodeName) {
    const at = text.toLowerCase().indexOf(String(nodeName).toLowerCase());
    if (at !== -1) { start = at; end = at + nodeName.length; }
  }

  if (start === null || start >= text.length) {
    return { text: text, trimmed: false };
  }

  // Walk out to sentence boundaries. Newlines count: in extracted PDF text a
  // heading is its own line and rarely ends in a full stop.
  const isBoundary = (i) =>
    ".!?\n".indexOf(text[i]) !== -1;

  let from = start;
  while (from > 0 && !isBoundary(from - 1)) from--;
  let to = end;
  while (to < text.length && !isBoundary(to)) to++;
  if (to < text.length) to++;   // keep the closing punctuation

  // A single sentence can still be terse, so pull in a little either side —
  // but only up to the next boundary, never mid-word.
  const PAD = 220;
  let padFrom = from;
  while (padFrom > 0 && from - padFrom < PAD) padFrom--;
  while (padFrom > 0 && !isBoundary(padFrom - 1)) padFrom++;
  let padTo = to;
  while (padTo < text.length && padTo - to < PAD) padTo++;
  while (padTo < text.length && !isBoundary(padTo)) padTo++;
  if (padTo < text.length) padTo++;

  const slice = text.slice(padFrom, padTo).trim();
  const trimmed = slice.length < text.trim().length;

  return {
    text: (padFrom > 0 ? "… " : "") + slice + (padTo < text.length ? " …" : ""),
    trimmed: trimmed,
  };
}

/* Write `text` into `target`, wrapping occurrences of `term` in <mark>.
 *
 * The passage is often the whole chunk, so finding the node in it by eye is
 * real work. Matching is case-insensitive and on whole words only, so "AI"
 * does not light up inside "explain".
 */
function highlightInto(target, text, term) {
  const needle = (term || "").trim();
  if (!needle) {
    target.textContent = text;
    return;
  }

  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // \b fails next to punctuation and non-ASCII, so the boundary is asserted
  // with lookarounds on "letter or digit" instead.
  const re = new RegExp("(?<![\\p{L}\\p{N}])" + escaped + "(?![\\p{L}\\p{N}])", "giu");

  let last = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      target.appendChild(document.createTextNode(text.slice(last, match.index)));
    }
    const mark = document.createElement("mark");
    mark.textContent = match[0];
    target.appendChild(mark);
    last = match.index + match[0].length;
    if (match[0].length === 0) re.lastIndex++;   // guard against a zero-width match
  }
  if (last < text.length) {
    target.appendChild(document.createTextNode(text.slice(last)));
  }
}

/* --------------------------------------------------------------- events */
cy.on("tap", "node", (e) => openNode(e.target));

/* Hovering an edge says what it claims.
 *
 * It used to add the model's confidence and a reading of it -- "0.62 - the
 * model is unsure, worth checking". That belonged to a pipeline that guessed
 * per chunk. It also outlived the data it read: `state` was removed with the
 * draft/firm/inked styling, so the lookup returned undefined and every single
 * edge reported itself as unsure.
 *
 * What is worth saying is what the document claims, and why. */
const edgeTip = document.createElement("div");
edgeTip.className = "edge-tip";
edgeTip.hidden = true;
document.body.appendChild(edgeTip);

function placeTip(ev) {
  // Offset from the cursor, and flipped near the right or bottom edge so the
  // tooltip never falls off screen.
  const pad = 14;
  const rect = edgeTip.getBoundingClientRect();
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = ev.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = ev.clientY - rect.height - pad;
  edgeTip.style.left = x + "px";
  edgeTip.style.top = y + "px";
}

cy.on("mouseover", "edge", (e) => {
  const edge = e.target;

  edgeTip.replaceChildren();
  const line = document.createElement("div");
  line.appendChild(document.createTextNode(edge.source().data("name") + " "));
  const verb = document.createElement("span");
  verb.className = "verb";
  verb.textContent = edge.data("type").replace(/_/g, " ");
  line.appendChild(verb);
  line.appendChild(document.createTextNode(" " + edge.target().data("name")));
  edgeTip.appendChild(line);

  // The sentence the document was read from, when there is one. Document text
  // by way of the model, so textContent -- never an HTML string.
  const why = edge.data("why");
  if (why) {
    const note = document.createElement("div");
    note.className = "why";
    note.textContent = why;
    edgeTip.appendChild(note);
  }

  edgeTip.hidden = false;
  if (e.originalEvent) placeTip(e.originalEvent);
  edge.addClass("hovered");
});

cy.on("mousemove", "edge", (e) => {
  if (!edgeTip.hidden && e.originalEvent) placeTip(e.originalEvent);
});

cy.on("mouseout", "edge", (e) => {
  edgeTip.hidden = true;
  e.target.removeClass("hovered");
});
cy.on("tap", (e) => {
  if (e.target === cy) closePanel();
});



document.getElementById("panel-delete").addEventListener("click", async () => {
  const id = panel.dataset.nodeId;
  if (!id || !confirm("Delete this node and all its connections?")) return;
  await api(`/api/w/${CFG.workspaceId}/node/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  cy.getElementById(id).remove();
  closePanel();
  updateStatus();
});

/* The signature gesture: pencil becomes ink. */
document.getElementById("panel-confirm").addEventListener("click", async () => {
  const id = panel.dataset.nodeId;
  if (!id) return;
  await api(`/api/w/${CFG.workspaceId}/node/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ user_edited: true }),
  });
  const node = cy.getElementById(id);
  node.data("user_edited", true);
  node.connectedEdges().forEach((edge) => edge.data("user_edited", true));
  const chip = document.getElementById("panel-state");
  chip.dataset.state = "inked";
  chip.textContent = "edited by you";
  chip.hidden = false;
  updateStatus();
});

document.getElementById("zoom-in").addEventListener("click", () => cy.zoom(cy.zoom() * 1.3));
document.getElementById("zoom-out").addEventListener("click", () => cy.zoom(cy.zoom() / 1.3));
document.getElementById("zoom-fit").addEventListener("click", () => cy.fit(undefined, 60));
document.getElementById("relayout").addEventListener("click", () => runLayout());
for (const key of ["web", "flow"]) {
  const btn = document.getElementById("layout-" + key);
  if (btn) btn.addEventListener("click", () => runLayout(key));
}

document.getElementById("rail-toggle").addEventListener("click", () => {
  const app = document.querySelector(".app");
  app.dataset.rail = app.dataset.rail === "collapsed" ? "open" : "collapsed";
  setTimeout(() => cy.resize(), 220);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePanel();
  if (e.key === "f" && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
    cy.fit(undefined, 60);
  }
});

/* Poll while any document is still ingesting; stop once everything settles. */
async function pollJobs() {
  // `resolving` was missing here, so a document sat on "extracting" in the list
  // until something else triggered a refresh.
  const pending = document.querySelectorAll(
    '.doc[data-status="pending"], .doc[data-status="parsing"], ' +
    '.doc[data-status="extracting"], .doc[data-status="resolving"]');
  if (!pending.length) return;

  const docs = await api(`/api/w/${CFG.workspaceId}/documents`).catch(() => null);
  if (!docs) return;

  let anyReady = false;
  for (const doc of docs) {
    const el = document.querySelector(`.doc[data-id="${doc.id}"]`);
    if (!el) continue;
    if (el.dataset.status !== "ready" && doc.status === "ready") anyReady = true;
    el.dataset.status = doc.status;
    if (doc.error) el.dataset.error = doc.error;
    el.querySelector(".doc-meta").textContent =
      doc.status === "ready"
        ? `${doc.pages ?? "—"} pages`
        : (STAGE_WORDS[doc.status] || doc.status);
    el.querySelector(".doc-progress > i").style.width = `${doc.progress ?? 0}%`;
  }

  if (anyReady) await loadGraph();
  // Whether or not anything finished, the canvas may be showing this
  // document's progress and needs the new percentage.
  refreshCanvasView();
}

setInterval(pollJobs, 2500);

/* ==========================================================================
 * Editing modes
 *
 * Select / link / add change what a click means, so the active mode is stated
 * in the toolbar and a hint says what to do next. Nothing is left to be
 * inferred from a cursor change.
 * ========================================================================== */
let mode = "select";
let linkFrom = null;

const HINTS = {
  select: null,
  connect: "Click the first concept, then the second.",
  add: "Click anywhere on the canvas to place a concept.",
};

function setMode(next) {
  mode = next;
  linkFrom = null;
  for (const m of ["select", "connect", "add"]) {
    document.getElementById("m-" + m).setAttribute("aria-pressed", String(m === next));
  }
  const hint = document.getElementById("mode-hint");
  hint.textContent = HINTS[next] || "";
  hint.hidden = !HINTS[next];
}

for (const m of ["select", "connect", "add"]) {
  document.getElementById("m-" + m).addEventListener("click", () => setMode(m));
}

document.addEventListener("keydown", (e) => {
  if (/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
  if (e.key === "v") setMode("select");
  if (e.key === "c") setMode("connect");
  if (e.key === "n") setMode("add");
});

/* Place a concept where the canvas was clicked. */
cy.on("tap", async (e) => {
  if (mode !== "add" || e.target !== cy) return;
  const name = prompt("Name this concept");
  if (!name || !name.trim()) return;
  try {
    const created = await api("/api/w/" + CFG.workspaceId + "/node", {
      method: "POST",
      body: JSON.stringify({ name: name.trim(), type: "concept" }),
    });
    cy.add({
      data: { id: created.id, name: created.name, type: created.type, user_edited: true },
      position: e.position,
    });
    updateStatus();
    setMode("select");
  } catch (err) {
    alert(err.message);
  }
});

/* Link two concepts. */
cy.on("tap", "node", async (e) => {
  if (mode !== "connect") return;
  const node = e.target;
  const hint = document.getElementById("mode-hint");

  if (!linkFrom) {
    linkFrom = node;
    hint.textContent = 'From "' + node.data("name") + '" — now click the second concept.';
    return;
  }
  if (linkFrom.id() === node.id()) return;

  const type = prompt("How are they related?\n\n" + CFG.relationTypes.join(", "), "related_to");
  if (!type) { setMode("connect"); return; }

  try {
    await api("/api/w/" + CFG.workspaceId + "/edge", {
      method: "POST",
      body: JSON.stringify({ source: linkFrom.id(), target: node.id(), type: type.trim() }),
    });
    cy.add({
      data: {
        id: linkFrom.id() + "~" + type.trim() + "~" + node.id(),
        source: linkFrom.id(),
        target: node.id(),
        type: type.trim(),
        user_edited: true,
      },
    });
    updateStatus();
  } catch (err) {
    alert(err.message);
  }
  setMode("connect");
});

/* ==========================================================================
 * Node colour
 *
 * Colour normally encodes trust (draft / firm / inked), so a user-chosen
 * colour is an override: a way to mark "these belong together" for a reason
 * only the reader knows. Clearing it returns the node to the trust encoding.
 * ========================================================================== */
/* Six colours a user can apply, none of which is one the canvas already uses
   to mean something. Blue (#3987e5) and orange (#d95926) were on this list and
   are also "a link" and "read first" -- so a hand-coloured edge could not be
   told apart from one the graph had labelled, which is what made the canvas
   look arbitrary. Colour from the user and colour from the graph must never be
   the same colour. */
const SWATCHES = ["#2bb3c0", "#1baf7a", "#b8933a", "#e04b6a", "#c2618c", "#8a6fd1"];

/* Colour whatever is selected.
 *
 * Colour is now the only thing a line or a circle encodes that the model did
 * not decide -- line style used to carry the model's confidence, and with a
 * model that reads the whole document that question is settled before
 * anything is drawn. So this acts on the selection rather than on the one
 * concept a panel happens to be showing: select a box of related things and
 * give them all a colour.
 */
function paintSelectionBar() {
  const nodes = cy.nodes(":selected").not(".filtered");
  const edges = cy.edges(":selected").not(".filtered");
  const bar = document.getElementById("select-bar");
  const total = nodes.length + edges.length;

  if (!total) {
    bar.hidden = true;
    return;
  }

  const bits = [];
  if (nodes.length) bits.push(nodes.length + (nodes.length === 1 ? " concept" : " concepts"));
  if (edges.length) bits.push(edges.length + (edges.length === 1 ? " link" : " links"));
  document.getElementById("select-count").textContent = bits.join(", ") + " selected";

  // Only repaint the swatches when the shared colour changed, so clicking
  // around a selection does not rebuild the row under the cursor.
  const colours = new Set([...nodes, ...edges].map((el) => el.data("color") || null));
  const shared = colours.size === 1 ? [...colours][0] : null;
  if (bar.dataset.shared !== String(shared)) {
    bar.dataset.shared = String(shared);
    renderSwatches(document.getElementById("select-swatches"), shared);
  }
  bar.hidden = false;
}

function renderSwatches(box, current) {
  box.replaceChildren();
  for (const colour of SWATCHES) {
    const b = document.createElement("button");
    b.className = "swatch";
    b.style.background = colour;
    b.setAttribute("aria-label", "Colour the selection " + colour);
    b.setAttribute("aria-pressed", String(current === colour));
    b.addEventListener("click", () => colourSelection(colour));
    box.appendChild(b);
  }
  const clear = document.createElement("button");
  clear.className = "swatch-clear";
  clear.textContent = "×";
  clear.title = "Back to the default colour";
  clear.setAttribute("aria-label", "Clear the colour");
  clear.addEventListener("click", () => colourSelection(null));
  box.appendChild(clear);
}

async function colourSelection(colour) {
  const nodes = cy.nodes(":selected").not(".filtered");
  const edges = cy.edges(":selected").not(".filtered");

  // Paint first, save after: on a boxful of elements the round trips are
  // slower than the eye, and a colour that lags behind the click feels broken.
  // Setting the data is the whole job: both the node border and the edge line
  // read `color` from the stylesheet, so the canvas repaints itself and the
  // same rule applies on the next reload. Inline styles here were what hid
  // the reload bug -- they made it look right until you came back.
  nodes.forEach((n) => n.data("color", colour));
  edges.forEach((e) => e.data("color", colour));
  document.getElementById("select-bar").dataset.shared = String(colour);
  renderSwatches(document.getElementById("select-swatches"), colour);

  const base = "/api/w/" + CFG.workspaceId;
  const saves = [
    ...nodes.map((n) =>
      api(base + "/node/" + encodeURIComponent(n.id()), {
        method: "PATCH",
        body: JSON.stringify({ color: colour }),
      })
    ),
    ...edges.map((e) =>
      api(base + "/edge", {
        method: "PATCH",
        body: JSON.stringify({
          source: e.data("source"),
          target: e.data("target"),
          // The stored phrasing, not a normalised form: the server matches on
          // it exactly so that recolouring cannot add an edge.
          type: e.data("type"),
          color: colour,
        }),
      })
    ),
  ];

  const failed = (await Promise.allSettled(saves)).filter((r) => r.status === "rejected");
  if (failed.length) {
    // The canvas already shows the new colour. Say plainly that the server
    // did not take it, rather than leaving a colour that vanishes on reload.
    alert(
      failed.length + " of " + saves.length +
      " could not be saved. Reload to see what stuck."
    );
  }
}

cy.on("select unselect", () => paintSelectionBar());
// A deleted element stays selected in the bar's count otherwise.
cy.on("remove", () => paintSelectionBar());

/* Fill the panel's "connected to" section when a node opens. */
function paintRelations(nodeId) {
  const box = document.getElementById("panel-related");
  box.innerHTML = "";
  const node = cy.getElementById(nodeId);
  const edges = node.connectedEdges();

  if (!edges.length) {
    box.innerHTML = '<p class="source-none">Nothing links to this yet.</p>';
    return;
  }
  edges.forEach((edge) => {
    const outgoing = edge.source().id() === nodeId;
    const other = outgoing ? edge.target() : edge.source();
    const b = document.createElement("button");
    b.className = "related-item";
    const verb = document.createElement("span");
    verb.className = "related-verb";
    verb.textContent = (outgoing ? "" : "← ") + edge.data("type").replace(/_/g, " ");
    const name = document.createElement("span");
    name.textContent = other.data("name");
    b.append(verb, name);
    // Walking the graph by clicking through the panel, not just the canvas.
    b.addEventListener("click", () => {
      cy.center(other);
      openNode(other);
    });
    box.appendChild(b);
  });
}

/* ==========================================================================
 * Filtering the canvas to one document
 *
 * No request is made. Every entity already carries the list of documents it
 * came from -- `get_graph` returns it and the nodes hold it -- so narrowing to
 * one document is a client-side filter over data that is already here. Asking
 * the server would add a round trip to answer a question the browser can
 * already answer.
 *
 * An entity can belong to several documents: resolution merges the same
 * concept across them. So a node shows if the selected document is anywhere in
 * its list, and an edge shows only when both of its endpoints do.
 * ========================================================================== */
let activeDocument = null;

function applyDocumentFilter() {
  filterToDocument(cy, activeDocument);
  updateStatus();
}

function selectDocument(el) {
  const wasActive = el && el.getAttribute("aria-pressed") === "true";
  document
    .querySelectorAll(".doc")
    .forEach(function (d) { d.setAttribute("aria-pressed", "false"); });

  if (el && !wasActive) {
    el.setAttribute("aria-pressed", "true");
    activeDocument = el.dataset.id;
  } else {
    activeDocument = null;
  }

  applyDocumentFilter();
  closePanel();

  // Chat scopes itself to the same document, so it is told rather than made
  // to duplicate this logic.
  document.dispatchEvent(
    new CustomEvent("gf:document-selected", { detail: { id: activeDocument } })
  );

  // Re-run rather than just re-fit: with a component hidden, the remaining
  // ones should close the gap it left instead of keeping its empty seat.
  if (cy.nodes().not(".filtered").length) runLayout();
  else cy.fit(undefined, 60);

  refreshCanvasView();
}

document.getElementById("doc-list").addEventListener("click", function (e) {
  const doc = e.target.closest(".doc");
  if (doc) selectDocument(doc);
});




/* ==========================================================================
 * Removing a document
 *
 * Deletes the document and the subgraph it produced, in one step. Confirmation
 * names the file and says what goes with it, because the graph is the part
 * that took minutes to build and is the part people forget they are also
 * deleting.
 *
 * Entities shared with another document survive: the server removes only this
 * document's contribution (see delete_document_subgraph).
 * ========================================================================== */
document.getElementById("doc-list").addEventListener("click", async function (e) {
  const button = e.target.closest(".doc-delete");
  if (!button) return;
  e.stopPropagation();          // do not also select the row being removed

  const id = button.dataset.id;
  const name = button.dataset.name || "this document";
  if (!confirm('Remove "' + name + '" and the graph built from it?\n\nConcepts it shares with other documents are kept.')) {
    return;
  }

  button.disabled = true;
  try {
    await api("/api/w/" + CFG.workspaceId + "/documents/" + id, { method: "DELETE" });
  } catch (err) {
    button.disabled = false;
    alert(err.message);
    return;
  }

  const row = button.closest(".doc-row");
  if (row) row.remove();

  if (activeDocument === id) activeDocument = null;

  const count = document.getElementById("doc-count");
  if (count) count.textContent = document.querySelectorAll(".doc").length;

  // Reload rather than prune locally: the server decides which shared entities
  // survived, and guessing at that in the browser would drift from the truth.
  await loadGraph();
  applyDocumentFilter();
  closeDock();
});


/* Retry after a failed load. Aura's free tier pauses when idle and takes a few
   seconds to wake, so the honest recovery is to try again rather than make the
   user reload the page and lose their place. */
document.getElementById("canvas-retry").addEventListener("click", async function () {
  const button = this;
  button.disabled = true;
  button.textContent = "Trying…";
  try {
    await loadGraph();
  } catch (err) {
    showLoadError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Try again";
  }
});


/* ==========================================================================
 * Ingest progress, on the canvas
 *
 * A document being read has nothing to draw, and a blank canvas after an
 * upload reads as "it failed". This shows what is happening where the user is
 * already looking.
 *
 * The state belongs to the document, not to the moment: open another document
 * mid-ingest and you get its graph; come back and the progress is still here.
 * ========================================================================== */

/* The canvas is either showing a graph or it is not. Nothing is ever layered
   over it, so there is no half state to get wrong. */
function showCanvas(on) {
  document.getElementById("canvas").classList.toggle("is-hidden", !on);
}

function docIsReady(el) {
  return !el || el.dataset.status === "ready";
}

/* Decide what the canvas should show for the current selection. One place,
   so the graph and the progress panel can never both be on screen. */
function refreshCanvasView() {
  const selected = document.querySelector('.doc[aria-pressed="true"]');
  const progress = document.getElementById("canvas-progress");

  if (selected && !docIsReady(selected)) {
    const status = selected.dataset.status || "pending";
    progress.dataset.state = status;
    document.getElementById("progress-doc").textContent =
      selected.querySelector(".doc-name").textContent;
    document.getElementById("progress-stage").textContent =
      STAGE_WORDS[status] || "Working";

    const fill = selected.querySelector(".doc-progress > i");
    document.getElementById("progress-fill").style.width =
      status === "failed" ? "100%" : (fill ? fill.style.width : "0%");

    const note = progress.querySelector(".progress-note");
    note.textContent =
      status === "failed"
        ? (selected.dataset.error || "Remove it and try again, or upload a different file.")
        : "Building the graph. You can open another document while this runs.";

    progress.hidden = false;
    // Hide the canvas outright rather than trusting that it is empty. A
    // document part-way through ingestion can already have concepts written,
    // and the whole complaint was a graph showing through the progress panel.
    // One state at a time: either the document is being read, or it is drawn.
    showCanvas(false);
    showEmptyState(false);
    return;
  }

  progress.hidden = true;
  showCanvas(true);
  if (cy.nodes().not(".filtered").length === 0) showEmptyCanvas();
  else showEmptyState(false);
}



/* Uploading reloads the page. Without this the reload lands with nothing
   selected and the whole workspace on screen, which reads as "my upload did
   nothing". The id is handed across the reload in sessionStorage. */
(function selectJustUploaded() {
  let id = null;
  try { id = sessionStorage.getItem("gf:justUploaded"); } catch (e) { /* private mode */ }
  if (!id) return;
  try { sessionStorage.removeItem("gf:justUploaded"); } catch (e) { /* ignore */ }

  const el = document.querySelector('.doc[data-id="' + id + '"]');
  if (el) selectDocument(el);
})();

/* ==========================================================================
 * Resizing the side panels
 *
 * Source passages are often a whole chunk of a page, and a fixed 400px column
 * makes that a long thin ribbon. Both panels share one width so the layout
 * does not jump when switching between a node and the chat.
 * ========================================================================== */
(function makeResizable() {
  const MIN = 320;
  const MAX_FRACTION = 0.8;   // the canvas must never disappear entirely
  const dockEl = document.getElementById("dock");

  function setWidth(px) {
    const capped = Math.min(
      Math.max(Math.round(px), MIN),
      Math.round(window.innerWidth * MAX_FRACTION)
    );
    document.documentElement.style.setProperty("--panel-w", capped + "px");
    try { localStorage.setItem("gf:panelWidth", capped); } catch (e) { /* ignore */ }
    return capped;
  }

  try {
    const saved = parseInt(localStorage.getItem("gf:panelWidth") || "", 10);
    if (saved >= MIN) setWidth(saved);
  } catch (e) { /* storage unavailable; the default width is fine */ }

  const grip = document.createElement("div");
  grip.className = "panel-grip";
  grip.setAttribute("role", "separator");
  grip.setAttribute("aria-orientation", "vertical");
  grip.setAttribute("aria-label", "Resize panel");
  grip.tabIndex = 0;

  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = dockEl.getBoundingClientRect().width;
    // Capture on the grip: without it a fast drag leaves the pointer behind
    // and the panel stops following.
    grip.setPointerCapture(e.pointerId);

    const onMove = (ev) => setWidth(startWidth + (startX - ev.clientX));
    const onUp = () => {
      grip.releasePointerCapture(e.pointerId);
      grip.removeEventListener("pointermove", onMove);
      grip.removeEventListener("pointerup", onUp);
      document.body.style.userSelect = "";
      cy.resize();
    };
    // Without this, dragging selects text across the whole page.
    document.body.style.userSelect = "none";
    grip.addEventListener("pointermove", onMove);
    grip.addEventListener("pointerup", onUp);
  });

  grip.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const step = e.shiftKey ? 48 : 16;
    setWidth(dockEl.getBoundingClientRect().width + (e.key === "ArrowLeft" ? step : -step));
    cy.resize();
  });

  dockEl.appendChild(grip);
})();


/* ==========================================================================
 * Start
 *
 * Last, deliberately. The first load calls refreshCanvasView, which reads
 * STAGE_WORDS — a `const`, so it is not hoisted. Called from higher up this
 * file it only worked because the await inside loadGraph yielded long enough
 * for the rest of the script to parse: true, but a race waiting to be lost.
 * ========================================================================== */
loadGraph().catch((err) => {
  showLoadError(err.message);
});
