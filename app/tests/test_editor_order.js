/* Guards editor.js against the temporal dead zone.
 *
 *     node tests/test_editor_order.js
 *
 * editor.js is one long script with no module system: it declares its state
 * and helpers at the top level, and a few statements at the top level actually
 * run the moment the browser parses the file. `const` and `let` are not
 * hoisted, so a statement that runs during parse and reads a `const` declared
 * further down throws a ReferenceError -- which aborts the whole script, and
 * takes every listener and the initial graph load with it. The page then just
 * sits there, with no error anywhere the user can see.
 *
 * That has now happened twice while this file grew by appending. The rule that
 * prevents it is simple enough to check mechanically: nothing may execute at
 * the top level until every top-level declaration has been made. Event
 * listeners are exempt -- registering one is not running it.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const file = path.join(__dirname, "..", "src", "graphforge", "static", "editor.js");
const src = fs.readFileSync(file, "utf8");
const lineOf = (pos) => src.slice(0, pos).split("\n").length;

/* Top-level declarations: a line starting at column 0 with const/let/var.
   Indented ones are inside a function and irrelevant here. */
let lastDecl = { name: null, pos: -1 };
for (const m of src.matchAll(/^(?:const|let|var) (\w+)/gm)) {
  if (m.index > lastDecl.pos) lastDecl = { name: m[1], pos: m.index };
}

/* Top-level statements that execute during parse. `document.x.addEventListener`
   and `setInterval` only schedule work, so they are not parse-time reads. */
const runsNow = [];
for (const m of src.matchAll(/^\(function (\w+)?/gm)) {
  runsNow.push({ what: `IIFE ${m[1] || "(anonymous)"}`, pos: m.index });
}
for (const m of src.matchAll(/^([A-Za-z_$][\w$]*)\(/gm)) {
  if (["if", "for", "while", "switch", "catch", "setInterval", "setTimeout"].includes(m[1])) continue;
  runsNow.push({ what: `call ${m[1]}()`, pos: m.index });
}

const early = runsNow.filter((s) => s.pos < lastDecl.pos);

let failed = 0;
if (early.length) {
  failed = 1;
  console.log("FAIL  top-level code runs before all declarations are made");
  console.log(`      last top-level declaration: ${lastDecl.name} (line ${lineOf(lastDecl.pos)})`);
  for (const s of early) {
    console.log(`      ${s.what} runs at line ${lineOf(s.pos)} -- move it below line ${lineOf(lastDecl.pos)}`);
  }
} else {
  console.log(`ok    ${runsNow.length} top-level statement(s), all after the last declaration`);
  console.log(`      (last declaration: ${lastDecl.name}, line ${lineOf(lastDecl.pos)})`);
}

/* A parse error would defeat the check above, so confirm the file is valid JS. */
try {
  new (require("vm").Script)(src, { filename: file });
  console.log("ok    editor.js parses");
} catch (e) {
  failed = 1;
  console.log(`FAIL  editor.js does not parse: ${e.message}`);
}


/* Nothing may read a data key that nothing writes.
 *
 * When the draft/firm/inked styling was removed, `state` stopped being set on
 * edges -- but the hover tooltip still read it. `undefined` fell through its
 * ladder to the last branch, so every edge on the canvas reported "the model
 * is unsure", including ones the user had confirmed. It failed silently, and
 * read as a wording problem rather than a dead lookup.
 *
 * A key that is read and never written is that bug, every time.
 */
const written = new Set();
// Any `key:` in the file, plus explicit el.data("key", value) writes.
// Deliberately loose: a tighter scan of the `data:` literals kept mis-reading
// them, because a template literal inside one contains its own braces. Loose
// still catches what matters -- a key that was deleted outright stops
// appearing anywhere, which is exactly how `state` went dangling.
for (const m of src.matchAll(/(\w+)\s*:/g)) written.add(m[1]);
for (const m of src.matchAll(/\.data\("(\w+)",/g)) written.add(m[1]);

const read = new Set();
for (const m of src.matchAll(/\.data\("(\w+)"\)/g)) read.add(m[1]);

const dangling = [...read].filter((k) => !written.has(k));
if (dangling.length) {
  failed = 1;
  console.log("FAIL  data key read but never set: " + dangling.join(", "));
} else {
  console.log(`ok    ${read.size} data key(s) read, all of them set somewhere`);
}

process.exit(failed);
