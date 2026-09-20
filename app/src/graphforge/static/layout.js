/* How the graph gets arranged on the canvas.
 *
 * Kept out of editor.js because the interesting part here is geometry, not
 * interface, and geometry can be checked: tests/test_graph_layout.js runs this
 * against a real extracted graph headlessly and counts overlapping nodes.
 *
 * **What a real graph looks like.** A document does not produce one tidy
 * connected tree. An 82-concept paper from this pipeline produces twelve
 * separate pieces -- one of 41 concepts and eleven of 2 to 9 -- and 58 of the
 * 82 concepts are mentioned exactly once, hanging off a handful of hubs. Both
 * problems below come from that shape.
 *
 * **Pieces piled up.** Handed all twelve pieces at once, a cytoscape layout
 * arranges them around a shared origin and lets them land on top of each
 * other, having no notion that they are unrelated. So no layout is ever run
 * over the whole graph: each piece is laid out in its own box and the boxes
 * are tiled. Two pieces then cannot collide whatever happens inside them --
 * the guarantee comes from the tiling, not from tuning a force simulation.
 *
 * **Levels ran off the page.** `breadthfirst` puts every node at its distance
 * from a root, which for a hub with forty children is a single row forty wide:
 * a 3233x241px ribbon, technically correct and impossible to read. The flow
 * view is laid out here instead, wrapping a wide level into a block.
 */
"use strict";

/* Two ways to read the same graph.
 *
 * `web` is force-directed: good for seeing which concepts cluster together.
 * `flow` runs top-to-bottom along the direction of the edges, so "A is_a B,
 * B part_of C" reads down the page like a sentence.
 */
const LAYOUTS = {
  web: {
    name: "cose",
    animate: false,
    // Tuned for one piece rather than a whole graph. The previous repulsion of
    // 24000 against gravity 1.2 was fighting itself: enough push to scatter
    // the nodes, enough pull to gather them back into a pile.
    nodeRepulsion: 10000,
    idealEdgeLength: 95,
    edgeElasticity: 70,
    nodeOverlap: 16,
    gravity: 0.35,
    numIter: 2500,
    // Most nodes hang off a hub, and from a fixed start those spokes come out
    // stacked on each other. A random start unsticks them.
    randomize: true,
    // Push labels apart, not just node circles. Only has an effect where text
    // can be measured, so it does nothing under the headless test -- the
    // spacing below is what that test actually holds to account.
    nodeDimensionsIncludeLabels: true,
    fit: false,
    padding: 0,
  },
  // Not a cytoscape layout name: positions come from layeredPositions below.
  flow: { name: "layered" },
};

/* ------------------------------------------------------------------ size -- */

/* How central a concept is, as a node diameter.
 *
 * Nothing in the data says "this is the main topic", but how many
 * relationships a concept has is a good proxy and costs the reader nothing.
 * In that 82-concept paper, 58 concepts are mentioned once and three carry 8
 * to 14 relationships each; those three are what the paper is about, and at a
 * uniform 20px they looked exactly like the other 79.
 *
 * Square root, so the busiest concept is clearly the biggest without the rest
 * collapsing into identical dots. Only visible edges count, so sizes still
 * mean something when the view is filtered to a single document.
 */
function sizeFor(node) {
  const deg = node.connectedEdges().not(".filtered").length;
  return Math.round(Math.min(54, 17 + Math.sqrt(deg) * 10.5));
}

/* Labels follow size, or the biggest node on the canvas is captioned in the
   same whisper as one mentioned once. */
function labelSizeFor(node) {
  const deg = node.connectedEdges().not(".filtered").length;
  if (deg >= 6) return 13;
  if (deg >= 3) return 11.5;
  return 10;
}

/* ------------------------------------------------------------- tiling ----- */

/* One node's worth of room, label included. Every spacing figure below is in
   these units, so nothing can be laid out tighter than a node can be drawn. */
const CELL_W = 175;
const CELL_H = 125;
const GAP = 80;

/* Room per node in the web view, as the side of the box a piece is given.
 *
 * Measured rather than picked: at 155 a real 82-concept graph put 1-7 pairs of
 * names on top of each other, and the count falls as this rises. Past roughly
 * 240 the gain flattens and the only thing that changes is how far you have to
 * pan, so this is where those two meet.
 */
const WEB_ROOM = 240;

/* Tile boxes so none of them overlap.
 *
 * Shelf packing: biggest first, filling a row left to right and wrapping when
 * the row passes a target width chosen to keep the whole arrangement roughly
 * square -- a long thin strip of pieces is as hard to read as a pile.
 *
 * Pure, and the function the no-overlap guarantee rests on, so it is tested on
 * its own rather than inferred from where nodes happened to land.
 */
function shelfPack(boxes) {
  const placed = boxes.slice().sort((a, b) => b.w * b.h - a.w * a.h);
  const area = placed.reduce((sum, b) => sum + b.w * b.h, 0);
  const target = Math.max(placed.length ? placed[0].w : 0, Math.sqrt(area) * 1.35);

  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const box of placed) {
    if (x > 0 && x + box.w > target) {
      x = 0;
      y += rowHeight + GAP;
      rowHeight = 0;
    }
    box.x = x;
    box.y = y;
    x += box.w + GAP;
    rowHeight = Math.max(rowHeight, box.h);
  }
  return placed;
}

/* -------------------------------------------------------------- flow ------ */

/* Distance of every node from a root, along the edges. */
function depthsOf(comp) {
  const nodes = comp.nodes();
  const depth = new Map();

  // Roots first, so the piece reads in the direction of its edges. A piece
  // that is one closed cycle has no root; start somewhere rather than fail.
  let frontier = nodes.filter((n) => n.indegree(false) === 0);
  if (!frontier.length) frontier = nodes.slice(0, 1);
  frontier.forEach((n) => depth.set(n.id(), 0));

  let current = frontier;
  let d = 0;
  while (current.length && d <= nodes.length) {
    d += 1;
    const next = [];
    current.forEach((n) => {
      n.outgoers("node").intersection(comp).forEach((m) => {
        if (!depth.has(m.id())) {
          depth.set(m.id(), d);
          next.push(m);
        }
      });
    });
    current = next;
  }
  // Anything a cycle kept us from reaching still needs somewhere to stand.
  nodes.forEach((n) => {
    if (!depth.has(n.id())) depth.set(n.id(), d + 1);
  });
  return depth;
}

/* Place one piece in levels, wrapping any level too wide to read.
 *
 * Positions are on a fixed grid, so nodes in this view cannot overlap by
 * construction -- no force simulation is involved and there is nothing to
 * converge. Returns positions relative to the centre of the piece.
 */
function layeredPositions(comp) {
  const depth = depthsOf(comp);
  const byLevel = new Map();
  comp.nodes().forEach((n) => {
    const d = depth.get(n.id());
    if (!byLevel.has(d)) byLevel.set(d, []);
    byLevel.get(d).push(n);
  });

  // Wide enough that a level rarely wraps, narrow enough that when one does
  // wrap the block stays roughly as wide as it is tall.
  const maxCols = Math.max(4, Math.round(Math.sqrt(comp.nodes().length) * 1.6));

  const pos = new Map();
  let y = 0;
  let widest = 0;
  const levels = Array.from(byLevel.keys()).sort((a, b) => a - b);

  for (const level of levels) {
    // Busiest concepts to the left of their level, so the eye meets the ones
    // that matter first and a wrapped level keeps them on the first row.
    const inLevel = byLevel.get(level).sort((a, b) => b.degree(false) - a.degree(false));

    for (let i = 0; i < inLevel.length; i += maxCols) {
      const row = inLevel.slice(i, i + maxCols);
      const rowWidth = (row.length - 1) * CELL_W;
      row.forEach((n, col) => pos.set(n.id(), { x: col * CELL_W - rowWidth / 2, y }));
      widest = Math.max(widest, rowWidth);
      y += CELL_H;
    }
    // A little air between levels, so a wrapped level still reads as one level.
    y += CELL_H * 0.35;
  }

  return { pos, w: widest + CELL_W, h: Math.max(CELL_H, y) };
}

/* ---------------------------------------------------- showing one document -- */

/* Hide everything that does not belong to the open document.
 *
 * Lives here, and is pure, because it has to run in exactly one more place
 * than is obvious. Loading the graph replaces every element on the canvas --
 * `remove()` then `add()` -- and a fresh element carries no classes, so the
 * filter is gone. Miss that and two documents are drawn on top of each other:
 * the symptom is a second graph appearing under the first the moment an
 * upload finishes, because that is when the graph reloads.
 *
 * A node can belong to several documents, since resolution merges the same
 * concept across them, so it shows when the open document is anywhere in its
 * list. An edge shows only when both of its ends do. A node with no documents
 * at all was added by hand: it is the user's, not the document's, and hiding
 * it would read as having deleted it.
 */
function filterToDocument(cy, documentId) {
  if (!documentId) {
    cy.elements().removeClass("filtered");
    return;
  }

  cy.nodes().forEach((node) => {
    const docs = node.data("documents") || [];
    node.toggleClass("filtered", docs.length > 0 && docs.indexOf(documentId) === -1);
  });

  cy.edges().forEach((edge) => {
    edge.toggleClass(
      "filtered",
      edge.source().hasClass("filtered") || edge.target().hasClass("filtered")
    );
  });
}

/* ----------------------------------------------------------- arrange ----- */

/* Lay out each connected piece on its own, tile the pieces, then frame it. */
function arrange(cy, eles, layout) {
  const comps = eles.components().filter((c) => c.nodes().length > 0);
  if (!comps.length) return;

  const layered = layout.name === "layered";

  const plans = comps.map((comp) => {
    if (layered) {
      const placed = layeredPositions(comp);
      return { comp, pos: placed.pos, w: placed.w, h: placed.h };
    }
    const side = Math.sqrt(comp.nodes().length) * WEB_ROOM;
    return { comp, w: Math.max(220, side), h: Math.max(220, side) };
  });

  for (const box of shelfPack(plans)) {
    if (layered) {
      box.comp.nodes().forEach((n) => {
        const p = box.pos.get(n.id());
        n.position({ x: box.x + box.w / 2 + p.x, y: box.y + p.y });
      });
    } else {
      box.comp
        .layout({ ...layout, boundingBox: { x1: box.x, y1: box.y, w: box.w, h: box.h } })
        .run();
    }
  }

  // One fit over everything, once every piece has been placed.
  if (typeof cy.animate === "function") {
    cy.animate({ fit: { eles: eles, padding: 60 }, duration: 350 });
  } else {
    cy.fit(eles, 60);
  }
}

/* Available to the layout test under node; the browser just gets the globals. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    LAYOUTS, sizeFor, labelSizeFor, shelfPack, arrange, filterToDocument,
    depthsOf, layeredPositions, GAP, CELL_W, CELL_H, WEB_ROOM,
  };
}
