/* A small Markdown renderer for chat answers.
 *
 * Why not `marked` + `DOMPurify`: what is being rendered is model output
 * derived from an uploaded document, so it is untrusted twice over. Both of
 * those libraries produce an HTML *string* that then has to be sanitised and
 * assigned with innerHTML — safe only for as long as the sanitiser is right.
 *
 * This builds DOM nodes directly and puts every piece of model text through
 * `textContent`. There is no HTML string anywhere in the pipeline, so markup in
 * the answer cannot become markup on the page. Not sanitised: incapable.
 *
 * The cost is a deliberately small feature set — headings, emphasis, code,
 * lists, and paragraphs. Raw HTML, images and links are not supported, which
 * for an assistant answering from one document is no loss: a link it invented
 * would be a link worth not having.
 */
"use strict";

(function () {
  const INLINE = [
    // Order matters: code first, so emphasis markers inside a code span stay literal.
    { re: /`([^`]+)`/, tag: "code" },
    { re: /\*\*([^*]+)\*\*/, tag: "strong" },
    { re: /__([^_]+)__/, tag: "strong" },
    { re: /\*([^*]+)\*/, tag: "em" },
    { re: /_([^_]+)_/, tag: "em" },
  ];

  /* Split a line into text and inline-formatted spans. Recursive so that
     bold inside a sentence keeps the text either side of it. */
  function inline(target, text) {
    let earliest = null;

    for (const rule of INLINE) {
      const m = rule.re.exec(text);
      if (m && (earliest === null || m.index < earliest.m.index)) {
        earliest = { m, tag: rule.tag };
      }
    }

    if (!earliest) {
      target.appendChild(document.createTextNode(text));
      return;
    }

    const { m, tag } = earliest;
    if (m.index > 0) {
      target.appendChild(document.createTextNode(text.slice(0, m.index)));
    }
    const el = document.createElement(tag);
    // textContent, never innerHTML: this is the line that makes markup inert.
    el.textContent = m[1];
    target.appendChild(el);

    const rest = text.slice(m.index + m[0].length);
    if (rest) inline(target, rest);
  }

  function render(markdown) {
    const root = document.createDocumentFragment();
    const lines = String(markdown || "").split("\n");

    let list = null;      // the <ul>/<ol> currently being filled
    let para = null;      // the <p> currently being filled
    let fence = null;     // the <pre> currently being filled

    function endParagraph() { para = null; }
    function endList() { list = null; }

    for (const line of lines) {
      // fenced code block
      if (/^\s*```/.test(line)) {
        if (fence) {
          fence = null;
        } else {
          endParagraph(); endList();
          fence = document.createElement("pre");
          root.appendChild(fence);
        }
        continue;
      }
      if (fence) {
        fence.textContent += (fence.textContent ? "\n" : "") + line;
        continue;
      }

      if (!line.trim()) { endParagraph(); endList(); continue; }

      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        endParagraph(); endList();
        // Answers sit inside a panel, so start at h4 rather than h1 — the
        // page already has its own heading levels above this.
        const h = document.createElement("h" + Math.min(6, heading[1].length + 3));
        inline(h, heading[2]);
        root.appendChild(h);
        continue;
      }

      const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        endParagraph();
        const wanted = bullet ? "UL" : "OL";
        if (!list || list.tagName !== wanted) {
          list = document.createElement(wanted.toLowerCase());
          root.appendChild(list);
        }
        const li = document.createElement("li");
        inline(li, (bullet || numbered)[1]);
        list.appendChild(li);
        continue;
      }

      endList();
      if (!para) {
        para = document.createElement("p");
        root.appendChild(para);
      } else {
        // A wrapped line continues the same paragraph.
        para.appendChild(document.createTextNode(" "));
      }
      inline(para, line);
    }

    return root;
  }

  /* Replace an element's contents with rendered Markdown. */
  window.renderMarkdown = function (el, markdown) {
    el.replaceChildren(render(markdown));
  };
})();
