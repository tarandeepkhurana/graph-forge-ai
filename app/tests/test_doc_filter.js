/* Showing one document at a time.
 *
 *     node tests/test_doc_filter.js
 *
 * Reported as "I uploaded a pdf and saw the new graph with the previous pdf's
 * graph below it". The filter itself was right; it was being thrown away.
 * Loading the graph replaces every element -- remove() then add() -- and a new
 * element carries no classes, so the filter had to be re-applied afterwards
 * and was not. Clicking a document in the sidebar fixed it, because that path
 * did re-apply it, which made the bug look intermittent.
 *
 * So the test that matters is not "does the filter work" but "does it survive
 * a reload", which is the sequence every upload goes through.
 */
"use strict";

const path = require("path");
const cytoscape = require(path.join(__dirname, "..", "src", "graphforge", "static", "vendor", "cytoscape.min.js"));
const { filterToDocument } = require(path.join(__dirname, "..", "src", "graphforge", "static", "layout.js"));

const A = "doc-a";
const B = "doc-b";

// Two documents, one concept shared between them (resolution merges those),
// and one concept the user added by hand that belongs to neither.
const ELEMENTS = () => ({
  nodes: [
    { data: { id: "a1", name: "A only", documents: [A] } },
    { data: { id: "a2", name: "A too", documents: [A] } },
    { data: { id: "b1", name: "B only", documents: [B] } },
    { data: { id: "b2", name: "B too", documents: [B] } },
    { data: { id: "both", name: "Shared", documents: [A, B] } },
    { data: { id: "mine", name: "Hand-added", documents: [] } },
  ],
  edges: [
    { data: { id: "e1", source: "a1", target: "a2" } },
    { data: { id: "e2", source: "b1", target: "b2" } },
    { data: { id: "e3", source: "a1", target: "both" } },
    { data: { id: "e4", source: "a1", target: "b1" } },   // spans both documents
  ],
});

const build = () => cytoscape({ headless: true, styleEnabled: true, elements: ELEMENTS() });
const visible = (cy, sel) => cy.$(sel).not(".filtered").map((el) => el.id()).sort();

let failed = 0;
function check(ok, label, detail) {
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) failed = 1;
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// --- the rule itself ------------------------------------------------------
{
  const cy = build();
  filterToDocument(cy, A);
  check(same(visible(cy, "node"), ["a1", "a2", "both", "mine"]),
        "only the open document's concepts show", visible(cy, "node").join(", "));
  check(same(visible(cy, "edge"), ["e1", "e3"]),
        "an edge needs both ends on screen", visible(cy, "edge").join(", "));
  check(!cy.$("#mine").hasClass("filtered"),
        "a hand-added concept is never hidden by a document filter");
}

{
  const cy = build();
  filterToDocument(cy, B);
  check(same(visible(cy, "node"), ["b1", "b2", "both", "mine"]),
        "switching documents switches the graph", visible(cy, "node").join(", "));
}

{
  const cy = build();
  filterToDocument(cy, A);
  filterToDocument(cy, null);
  check(cy.elements().not(".filtered").length === cy.elements().length,
        "clearing the selection brings everything back");
}

// --- the sequence that actually broke -------------------------------------
// An upload finishes, the graph reloads, and every element is replaced.
{
  const cy = build();
  filterToDocument(cy, A);

  cy.elements().remove();
  cy.add(ELEMENTS());
  check(cy.elements(".filtered").length === 0,
        "a reload really does discard the filter", "which is why it must be re-applied");

  // loadGraph() re-applies it before laying anything out.
  filterToDocument(cy, A);
  check(same(visible(cy, "node"), ["a1", "a2", "both", "mine"]),
        "one document still, after the graph reloads",
        visible(cy, "node").join(", "));
  check(!visible(cy, "node").includes("b1") && !visible(cy, "node").includes("b2"),
        "the other document's graph does not come back with it");
}

// --- a document still being read -----------------------------------------
// Nothing of it is written yet, so nothing should be on the canvas: that is
// what left the previous graph showing behind the progress panel.
{
  const cy = build();
  filterToDocument(cy, "doc-c-still-ingesting");
  check(visible(cy, "node").join(",") === "mine",
        "a document with nothing extracted yet shows no other document's work",
        visible(cy, "node").join(", ") || "(nothing)");
}

// --- and the call site that was missing -----------------------------------
// The rule above is only ever wrong in one way: not being called after a
// reload. Check the source, because no unit test of a pure function can.
const fs = require("fs");
const src = fs.readFileSync(
  path.join(__dirname, "..", "src", "graphforge", "static", "editor.js"), "utf8");
const load = src.slice(src.indexOf("async function loadGraph"),
                       src.indexOf("async function loadGraph") + 2000);
const applyAt = load.indexOf("applyDocumentFilter()");
const layoutAt = load.indexOf("runLayout()");
check(applyAt > 0, "loadGraph re-applies the document filter",
      applyAt > 0 ? "" : "this is the bug: a reload shows every document at once");
check(applyAt > 0 && layoutAt > 0 && applyAt < layoutAt,
      "it does so before laying the graph out",
      "otherwise the hidden concepts leave their gaps in the arrangement");

// The progress panel must clear the canvas rather than sit on top of it.
check(src.includes("showCanvas(false)"),
      "the canvas is hidden while a document is being read");

console.log(failed ? "\ndocument filter tests FAILED" : "\nall document filter tests passed");
process.exit(failed);
