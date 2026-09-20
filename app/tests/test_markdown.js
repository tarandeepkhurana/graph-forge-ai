/* Tests for the chat Markdown renderer.
 *
 *     node tests/test_markdown.js
 *
 * The renderer handles model output derived from uploaded documents, so the
 * claim it has to earn is that markup in an answer can never become markup on
 * the page. A DOM shim just large enough to run it lets that be checked
 * without a browser: every node the renderer creates is inspected, and any
 * text it was given must come back as a text node, never as an element.
 */
"use strict";

// ---------------------------------------------------------------- DOM shim --
class Node {
  constructor(tag) {
    this.tagName = tag ? tag.toUpperCase() : null;
    this.children = [];
    this._text = "";
    this.dataset = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(frag) { this.children = frag.children.slice(); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text;
  }
}
class TextNode {
  constructor(text) { this.tagName = null; this._text = String(text); }
  get textContent() { return this._text; }
}

global.document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (t) => new TextNode(t),
  createDocumentFragment: () => new Node(null),
};
global.window = {};
require("../src/graphforge/static/markdown.js");

// ------------------------------------------------------------------ helpers --
function parse(md) {
  const host = new Node("div");
  window.renderMarkdown(host, md);
  return host;
}
function tags(node, out = []) {
  for (const child of node.children || []) {
    if (child.tagName) out.push(child.tagName.toLowerCase());
    tags(child, out);
  }
  return out;
}

let failures = 0;
function check(name, ok, note = "") {
  console.log(`  ${ok ? "ok " : "BAD"} ${name}${note ? "   " + note : ""}`);
  if (!ok) failures++;
}

// -------------------------------------------------------------------- tests --
console.log("formatting:");

let r = parse("Plain sentence.");
check("paragraph", tags(r).includes("p") && r.textContent.includes("Plain sentence."));

r = parse("Some **bold** and *italic* and `code`.");
check("emphasis + code", ["strong", "em", "code"].every((t) => tags(r).includes(t)),
  tags(r).join(","));
check("surrounding text kept", r.textContent === "Some bold and italic and code.",
  JSON.stringify(r.textContent));

r = parse("- one\n- two\n- three");
check("bullet list", tags(r).filter((t) => t === "li").length === 3 && tags(r).includes("ul"));

r = parse("1. first\n2. second");
check("numbered list", tags(r).includes("ol") && tags(r).filter((t) => t === "li").length === 2);

r = parse("## A heading\n\nBody text.");
check("heading", tags(r).some((t) => /^h[4-6]$/.test(t)));

r = parse("```\nline one\nline two\n```");
check("code fence", tags(r).includes("pre") && r.textContent.includes("line one\nline two"));

r = parse("Para one.\n\nPara two.");
check("blank line splits paragraphs", tags(r).filter((t) => t === "p").length === 2);

r = parse("A wrapped\nsentence.");
check("wrapped line stays one paragraph",
  tags(r).filter((t) => t === "p").length === 1 && r.textContent === "A wrapped sentence.");

r = parse("`**not bold**`");
check("code wins over emphasis", !tags(r).includes("strong"), tags(r).join(","));

// ---------------------------------------------------------------- injection --
console.log("\nuntrusted content — markup must stay inert:");

const HOSTILE = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<iframe src="javascript:alert(1)"></iframe>',
  '**bold** <b>raw</b>',
  '[click](javascript:alert(1))',
  '<svg/onload=alert(1)>',
  '</p><script>alert(1)</script><p>',
];

const ALLOWED = new Set([
  "p", "strong", "em", "code", "pre", "ul", "ol", "li", "h4", "h5", "h6",
]);

for (const payload of HOSTILE) {
  const out = parse(payload);
  const produced = tags(out);
  const bad = produced.filter((t) => !ALLOWED.has(t));
  const label = payload.length > 34 ? payload.slice(0, 34) + "…" : payload;
  check(
    `inert: ${label}`,
    bad.length === 0,
    bad.length ? "PRODUCED " + bad.join(",") : ""
  );
  // The dangerous text must survive as readable text, not vanish silently —
  // a user should see what the document tried to do.
  if (payload.includes("alert(1)")) {
    check(`  payload shown as text`, out.textContent.includes("alert(1)"));
  }
}

console.log(failures ? `\n${failures} failing` : "\nall markdown tests passed");
process.exit(failures ? 1 : 0);
