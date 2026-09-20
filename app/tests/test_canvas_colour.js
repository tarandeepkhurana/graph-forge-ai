/* Colour on the canvas: saved is not the same as shown.
 *
 *     node tests/test_canvas_colour.js
 *
 * Both bugs this guards were invisible until a reload, because the click-time
 * code painted the element directly. The colour reached Neo4j, the API sent it
 * back, and the canvas dropped it on the way in -- so it looked like saving was
 * broken when the only broken part was loading.
 *
 * These are source checks rather than behaviour checks: the stylesheet lives
 * inside a browser-only script that cannot be imported here. They are narrow on
 * purpose -- each one names a specific line that went missing.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "src", "graphforge", "static", "editor.js"), "utf8");

let failed = 0;
function check(ok, label, detail) {
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) failed = 1;
}

// --- the element has to carry the colour the server sent ------------------
const edgeMap = src.slice(src.indexOf("...data.edges.map"), src.indexOf("...data.edges.map") + 500);
check(/\bcolor:\s*e\.color/.test(edgeMap),
      "edges keep the colour the server sent",
      "without it the edge[color] rule matches nothing after a reload");

const nodeMap = src.slice(src.indexOf("...data.nodes.map"), src.indexOf("...data.nodes.map") + 500);
check(/\bcolor:\s*n\.color/.test(nodeMap), "nodes keep the colour the server sent");

// --- and the stylesheet has to read it ------------------------------------
const border = /"border-color":\s*\(n\)\s*=>[^,]*/.exec(src);
check(border !== null && /n\.data\("color"\)/.test(border[0]),
      "the node border reads its stored colour first",
      border ? border[0].replace(/\s+/g, " ").slice(0, 72) : "rule not found");

check(/selector:\s*"edge\[color\]"/.test(src), "there is a rule for a coloured edge");

// A user's colour must be declared after the graph's own edge colours, or the
// prerequisite rule would repaint it on every reload.
check(src.indexOf('selector: "edge[color]"') > src.indexOf('selector: "edge[?prerequisite]"'),
      "a user's edge colour is declared after the graph's own");

// --- the two palettes must not overlap ------------------------------------
// Blue means "a link" and orange means "read first". Offering either as a user
// colour makes a hand-coloured edge indistinguishable from a labelled one,
// which is what made the canvas look like it was colouring things at random.
const meaning = {};
for (const [, name, hex] of src.matchAll(/^\s*(firm|inked|pencil):\s*"(#[0-9a-fA-F]{6})"/gm)) {
  meaning[hex.toLowerCase()] = name;
}
const swatches = /const SWATCHES = \[([^\]]+)\]/.exec(src)[1]
  .match(/#[0-9a-fA-F]{6}/g).map((h) => h.toLowerCase());

const clash = swatches.filter((hex) => meaning[hex]);
check(clash.length === 0, "no user swatch is a colour the graph already uses",
      clash.length ? clash.map((h) => `${h} is "${meaning[h]}"`).join(", ")
                   : `${swatches.length} swatches, none reserved`);
check(new Set(swatches).size === swatches.length, "no swatch is offered twice");

console.log(failed ? "\ncolour tests FAILED" : "\nall colour tests passed");

// --- what a user's colour must NOT be able to erase ----------------------
// Colour is the user's; the graph's meaning is not. They have to live on
// different channels, or painting an edge silently turns a prerequisite into
// an ordinary link. Weight and arrowhead are the graph's channels.
const colourAt = src.indexOf('selector: "edge[color]"');
const colourRule = src.slice(colourAt, src.indexOf("},", colourAt));
check(colourAt > 0, "the coloured-edge rule is findable");
check(!colourRule.includes("width") && !colourRule.includes("arrow-shape"),
      "a colour cannot change an edge's weight or arrowhead",
      colourRule.split("style:")[1] ? colourRule.split("style:")[1].trim().slice(0, 70) : "");

const prereqAt = src.indexOf('selector: "edge[?prerequisite]"');
const prereqRule = src.slice(prereqAt, src.indexOf("},", prereqAt));
check(prereqRule.includes("target-arrow-shape"),
      "a prerequisite is marked by its arrowhead, not only its colour");
check(prereqRule.includes("width: 3"),
      "a prerequisite is drawn heavier than a plain link");

// A legend showing only colour stops being true the moment anyone recolours.
const html = fs.readFileSync(
  path.join(__dirname, "..", "src", "graphforge", "web", "templates", "workspace.html"), "utf8");
const legendAt = html.indexOf('class="legend"');
const legend = html.slice(legendAt, legendAt + 1400);
check(legend.includes("legend-mark") && legend.includes("polygon"),
      "the legend draws the arrowheads, not just coloured rules");

console.log(failed ? "colour tests FAILED" : "all colour checks passed");
process.exit(failed);
