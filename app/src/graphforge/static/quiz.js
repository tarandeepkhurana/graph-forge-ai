/* Quizzes: create dialog (workspace), results page (teacher), attempt form (student). */
"use strict";

(function () {
  const csrf = (document.cookie.match(/(?:^|;\s*)gf_csrf=([^;]*)/) || [])[1] || "";

  async function api(url, method, body) {
    const res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Something went wrong.");
    return data;
  }

  async function copy(input, button, label) {
    try {
      await navigator.clipboard.writeText(input.value);
      button.textContent = "Copied!";
      setTimeout(() => { button.textContent = label; }, 2000);
    } catch {
      input.select();
    }
  }

  // ---- create dialog in the workspace navbar ----
  const quizBtn = document.getElementById("quiz-btn");
  if (quizBtn) {
    const cfgEl = document.getElementById("gf-config");
    const cfg = cfgEl ? JSON.parse(cfgEl.textContent) : {};
    if (!(cfg.workspaceOwner === true || cfg.workspaceRole === "editor")) {
      quizBtn.hidden = true;
    }

    const modal = document.getElementById("quiz-modal");
    const doc = document.getElementById("quiz-doc");
    const count = document.getElementById("quiz-count");
    const create = document.getElementById("quiz-create");
    const status = document.getElementById("quiz-status");
    const linkWrap = document.getElementById("quiz-link-wrap");
    const linkInput = document.getElementById("quiz-link-input");
    const linkCopy = document.getElementById("quiz-link-copy");

    function open() {
      status.textContent = "";
      linkWrap.hidden = true;
      create.disabled = !doc.value;
      if (!doc.value) status.textContent = "Add a document and wait until it is ready first.";
      modal.hidden = false;
    }
    function close() { modal.hidden = true; }

    quizBtn.addEventListener("click", open);
    document.getElementById("quiz-close").addEventListener("click", close);
    document.getElementById("quiz-cancel").addEventListener("click", close);
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    linkCopy.addEventListener("click", () => copy(linkInput, linkCopy, "Copy"));

    create.addEventListener("click", async () => {
      create.disabled = true;
      linkWrap.hidden = true;
      status.textContent = "Writing questions from the document… this can take up to a minute.";
      try {
        const data = await api(`/api/w/${cfg.workspaceId}/quizzes`, "POST", {
          document_id: doc.value,
          num_questions: Number(count.value),
        });
        linkInput.value = location.origin + data.url;
        linkWrap.hidden = false;
        status.innerHTML = "";
        status.append(`“${data.title}” is ready with ${data.count} questions. Send this link to students. `);
        const see = document.createElement("a");
        see.href = `/quizzes/${data.id}`;
        see.textContent = "See questions and results";
        status.append(see);
      } catch (err) {
        status.textContent = err.message;
      } finally {
        create.disabled = false;
      }
    });
  }

  // ---- teacher results page ----
  const results = document.getElementById("quiz-results");
  if (results) {
    const id = results.dataset.quizId;
    const input = document.getElementById("quiz-share-link");
    const copyBtn = document.getElementById("quiz-share-copy");
    input.value = `${location.origin}/q/${id}`;
    copyBtn.addEventListener("click", () => copy(input, copyBtn, "Copy student link"));
    document.getElementById("quiz-delete").addEventListener("click", async () => {
      if (!confirm("Delete this quiz and all student responses?")) return;
      try {
        await api(`/api/quizzes/${id}`, "DELETE");
        location.href = "/quizzes";
      } catch (err) {
        alert(err.message);
      }
    });
  }

  // ---- student attempt form ----
  const form = document.getElementById("quiz-take");
  if (form) {
    const n = Number(form.dataset.count);
    const status = document.getElementById("quiz-take-status");
    const submit = document.getElementById("quiz-submit");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const answers = [];
      for (let i = 0; i < n; i++) {
        const picked = form.querySelector(`input[name="q${i}"]:checked`);
        answers.push(picked ? Number(picked.value) : -1);
      }
      const skipped = answers.filter((a) => a < 0).length;
      if (skipped && !confirm(`${skipped} question(s) unanswered. Submit anyway?`)) return;
      submit.disabled = true;
      status.textContent = "Submitting…";
      try {
        await api(`/api/quizzes/${form.dataset.quizId}/attempt`, "POST", { answers });
        location.reload();
      } catch (err) {
        status.textContent = err.message;
        submit.disabled = false;
      }
    });
  }
})();
