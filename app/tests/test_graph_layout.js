/* Does the canvas arrangement actually avoid overlap?
 *
 *     node tests/test_graph_layout.js
 *
 * The complaint this answers was "a lot of overlapping" on a real document,
 * and the previous arrangement code looked reasonable while producing a pile.
 * Reading a layout is not a way to tell; measuring one is. Cytoscape runs
 * headlessly, so the real library lays out the real extracted graph here and
 * every pair of nodes is checked for collision.
 *
 * The fixture in data/graph_sample.json is a graph this pipeline actually
 * produced: 82 concepts, 71 relationships, twelve disconnected pieces, and 58
 * of the concepts mentioned exactly once. The shape is the point -- a layout
 * that copes with one tidy connected tree is not being tested.
 */
"use strict";

const fs = require("fs");
const path = require("path");

/* The web layout starts from random positions, so the arrangement differs run
   to run -- measured across five runs the overlap count swung from 0 to 6,
   which is no basis for a threshold. Seeding makes the same graph produce the
   same arrangement every time, so a regression here is a real change and not
   the dice. */
let seed = 20260913;
Math.random = function () {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
};

const cytoscape = require(path.join(__dirname, "..", "src", "graphforge", "static", "vendor", "cytoscape.min.js"));
const { LAYOUTS, sizeFor, shelfPack, arrange, GAP } = require(path.join(__dirname, "..", "src", "graphforge", "static", "layout.js"));
const graph = JSON.parse(fs.readFileSync(path.join(__dirname, "data", "graph_sample.json"), "utf8"));

let failed = 0;
function check(ok, label, detail) {
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) failed = 1;
}

// ------------------------------------------------------------ pure packing --
// The no-overlap guarantee lives here, so test it directly and exhaustively
// rather than inferring it from where the nodes happened to land.
{
  const sizes = [41, 9, 5, 4, 4, 3, 3, 3, 3, 3, 2, 2];
  const boxes = shelfPack(sizes.map((n) => ({ w: 100 + n * 10, h: 80 + n * 8 })));
  let collisions = 0;
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      const apart =
        a.x + a.w + GAP <= b.x || b.x + b.w + GAP <= a.x ||
        a.y + a.h + GAP <= b.y || b.y + b.h + GAP <= a.y;
      if (!apart) collisions++;
    }
  }
  check(collisions === 0, "packed tiles never overlap", `${boxes.length} tiles, ${collisions} collisions`);

  const wide = shelfPack([{ w: 4000, h: 100 }, { w: 100, h: 100 }]);
  check(wide.every((b) => b.x >= 0 && b.y >= 0), "one oversized tile does not push others off-canvas");
}

// -------------------------------------------------------- the real arrangement --
function build() {
  return cytoscape({
    headless: true,
    styleEnabled: true,
    elements: {
      nodes: graph.nodes.map((n) => ({ data: n })),
      edges: graph.edges.map((e, i) => ({ data: { ...e, id: `e${i}` } })),
    },
    style: [{ selector: "node", style: { width: (n) => sizeFor(n), height: (n) => sizeFor(n) } }],
  });
}

/* Two nodes collide when their drawn circles intersect. A small allowance is
   subtracted: touching rims read as adjacent, not as a pile. */
function overlaps(cy) {
  const ns = cy.nodes().map((n) => ({
    x: n.position("x"), y: n.position("y"), r: sizeFor(n) / 2,
  }));
  let hits = 0;
  let worst = 0;
  for (let i = 0; i < ns.length; i++) {
    for (let j = i + 1; j < ns.length; j++) {
      const dx = ns[i].x - ns[j].x;
      const dy = ns[i].y - ns[j].y;
      const gap = Math.hypot(dx, dy) - (ns[i].r + ns[j].r);
      if (gap < 0) { hits++; worst = Math.min(worst, gap); }
    }
  }
  return { hits, worst: Math.round(worst), pairs: (ns.length * (ns.length - 1)) / 2 };
}

for (const name of Object.keys(LAYOUTS)) {
  const cy = build();
  arrange(cy, cy.elements(), LAYOUTS[name]);
  const { hits, worst, pairs } = overlaps(cy);
  check(hits === 0, `${name}: no two concepts overlap`,
        `${pairs} pairs checked${hits ? `, ${hits} overlapping, worst ${worst}px` : ""}`);

  // A pile is also visible as a collapsed bounding box: 82 nodes need room.
  const bb = cy.elements().boundingBox();
  check(bb.w > 900 && bb.h > 500, `${name}: arrangement is not collapsed`,
        `${Math.round(bb.w)} x ${Math.round(bb.h)}px`);
}

// ------------------------------------------------------------------ sizing --
{
  const cy = build();
  const degs = cy.nodes().map((n) => ({ deg: n.connectedEdges().length, size: sizeFor(n) }));
  const hub = degs.reduce((a, b) => (b.deg > a.deg ? b : a));
  const leaf = degs.reduce((a, b) => (b.deg < a.deg ? b : a));
  check(hub.size > leaf.size * 1.6, "the busiest concept is visibly bigger than a one-off",
        `degree ${hub.deg} -> ${hub.size}px vs degree ${leaf.deg} -> ${leaf.size}px`);
  check(hub.size <= 54, "no node grows without bound", `${hub.size}px`);
}

console.log(failed ? "\nlayout tests FAILED" : "\nall layout tests passed");

/* ------------------------------------------------------------------ labels --
 * Nodes not colliding is not the same as the canvas reading clearly: at 82
 * concepts, what actually overlaps is the names. Labels wrap to 96px under
 * each node, so their boxes are estimated from the same numbers the stylesheet
 * uses and checked the same way.
 */
function labelBoxes(cy) {
  return cy.nodes().map(function (n) {
    var size = sizeFor(n);
    var deg = n.connectedEdges().length;
    var font = deg >= 6 ? 13 : deg >= 3 ? 11.5 : 10;
    var name = String(n.data("name") || "");
    var perLine = Math.max(1, Math.floor(96 / (font * 0.55)));
    var lines = Math.max(1, Math.ceil(name.length / perLine));
    var w = Math.min(96, name.length * font * 0.55);
    var h = lines * font * 1.25;
    // text-valign is bottom and text-margin-y is 4 + size/4, per editor.js
    var top = n.position("y") + size / 2 + 4 + size / 4;
    return { x1: n.position("x") - w / 2, x2: n.position("x") + w / 2, y1: top, y2: top + h };
  });
}

Object.keys(LAYOUTS).forEach(function (name) {
  var cy = build();
  arrange(cy, cy.elements(), LAYOUTS[name]);
  var boxes = labelBoxes(cy);
  var hits = 0;
  for (var i = 0; i < boxes.length; i++) {
    for (var j = i + 1; j < boxes.length; j++) {
      var a = boxes[i], b = boxes[j];
      if (!(a.x2 <= b.x1 || b.x2 <= a.x1 || a.y2 <= b.y1 || b.y2 <= a.y1)) hits++;
    }
  }
  var pct = ((hits / boxes.length) * 100).toFixed(1);
  // Some crowding is unavoidable in a force-directed view and the reader can
  // zoom in; a name buried under another on more than a few nodes is not.
  check(hits <= Math.ceil(boxes.length * 0.05), name + ": names stay readable",
        hits + " overlapping label pairs across " + boxes.length + " names (" + pct + "%)");
});

console.log(failed ? "FAILED" : "done");
process.exit(failed);
