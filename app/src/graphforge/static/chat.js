/* Chat panel.
 *
 * Reads the SSE stream from POST /chat. Two kinds of message arrive on one
 * connection: `status` events narrating retrieval, and `token` events which are
 * the answer arriving a piece at a time.
 *
 * The retrieval trace is shown, not hidden behind a spinner. A grounded answer
 * is only worth as much as the evidence behind it, so the interface says what
 * was searched and what it read before it says anything else.
 */
"use strict";

(function () {
  const log = document.getElementById("chat-log");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");
  const csrf = document.cookie.match(/(?:^|;\s*)gf_csrf=([^;]*)/);
  const token = csrf ? csrf[1] : "";
  const workspaceId = JSON.parse(
    document.getElementById("gf-config").textContent
  ).workspaceId;

  // Opening and closing belong to the dock (editor.js); this file only fills
  // the chat view. Two owners of one panel's visibility is what made closing
  // "Ask" appear to close the node view as well.
  document.getElementById("tab-chat").addEventListener("click", function () {
    input.focus();
  });

  /* Chat answers from one document, so the scope is stated rather than
     assumed — and asking is disabled until a readable one is chosen. */
  function setScope() {
    const doc = document.querySelector('.doc[aria-pressed="true"]');
    const scope = document.getElementById("chat-scope");
    const ready = doc && doc.dataset.status === "ready";

    if (!doc) {
      scope.textContent = "Pick a document on the left";
    } else if (ready) {
      scope.textContent = doc.querySelector(".doc-name").textContent;
    } else {
      scope.textContent = "Still being read…";
    }
    send.disabled = !ready;
  }

  // Selection is owned by editor.js, which also filters the canvas. Two click
  // handlers on the same list would race over who sets aria-pressed first.
  document.addEventListener("gf:document-selected", setScope);

  /* One render per animation frame. Re-rendering on every token would redo the
     whole answer hundreds of times for a long reply and visibly jank. */
  let pending = null;
  function scheduleRender(el) {
    if (pending) return;
    pending = requestAnimationFrame(function () {
      pending = null;
      window.renderMarkdown(el, el.dataset.raw || "");
    });
  }

  function render(event, trace, answer, cites) {
    if (event.type === "status") {
      // Only the newest step is live; the rest settle into the trace.
      trace
        .querySelectorAll('[data-live="true"]')
        .forEach((s) => (s.dataset.live = "false"));
      const step = document.createElement("div");
      step.className = "trace-step";
      step.dataset.live = "true";
      step.textContent = event.message;
      trace.appendChild(step);
    } else if (event.type === "token") {
      // Tokens arrive mid-word and mid-syntax, so the raw text is kept and the
      // whole answer re-rendered. Rendering the fragment as it arrives would
      // show half-formed markup ("**bold" before its closing pair lands).
      answer.dataset.raw = (answer.dataset.raw || "") + event.text;
      scheduleRender(answer);
    } else if (event.type === "error") {
      answer.dataset.raw = "";
      answer.textContent = event.message;
    } else if (event.type === "done") {
      (event.sources || []).forEach(function (source) {
        const b = document.createElement("button");
        b.className = "cite";
        b.textContent =
          "[" + source.n + "] " + source.document +
          (source.page ? " p" + source.page : "");
        b.title = source.preview;
        cites.appendChild(b);
      });
    }
  }

  document.getElementById("chat-form").addEventListener("submit", async function (e) {
    e.preventDefault();
    const question = input.value.trim();
    const doc = document.querySelector('.doc[aria-pressed="true"]');
    if (!question || !doc) return;

    input.value = "";
    send.disabled = true;
    const empty = log.querySelector(".chat-empty");
    if (empty) empty.remove();

    const asked = document.createElement("div");
    asked.className = "msg msg-you";
    asked.textContent = question;
    log.appendChild(asked);

    const wrap = document.createElement("div");
    wrap.className = "msg";
    const trace = document.createElement("div");
    trace.className = "trace";
    const answer = document.createElement("div");
    answer.className = "msg-answer caret";
    const cites = document.createElement("div");
    cites.className = "cites";
    wrap.append(trace, answer, cites);
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;

    try {
      const res = await fetch("/api/w/" + workspaceId + "/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify({ document_id: doc.dataset.id, question: question }),
      });
      if (!res.ok) throw new Error("Could not reach the assistant (" + res.status + ")");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });

        // SSE frames are separated by a blank line; the tail may be partial.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";

        frames.forEach(function (frame) {
          const line = frame
            .split("\n")
            .find((l) => l.indexOf("data: ") === 0);
          if (!line) return;
          render(JSON.parse(line.slice(6)), trace, answer, cites);
          log.scrollTop = log.scrollHeight;
        });
      }
    } catch (err) {
      answer.textContent = err.message;
    } finally {
      if (pending) { cancelAnimationFrame(pending); pending = null; }
      if (answer.dataset.raw) window.renderMarkdown(answer, answer.dataset.raw);
      answer.classList.remove("caret");
      trace
        .querySelectorAll('[data-live="true"]')
        .forEach((s) => (s.dataset.live = "false"));
      send.disabled = false;
      input.focus();
    }
  });

  setScope();
})();
