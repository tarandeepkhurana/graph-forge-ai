const configElement = document.getElementById("gf-config");
const config = JSON.parse(configElement.textContent);
const workspaceId = config.workspaceId;

const shareBtn = document.getElementById("share-btn");
const shareModal = document.getElementById("share-modal");
const shareClose = document.getElementById("share-close");
const shareCancel = document.getElementById("share-cancel");
const shareSave = document.getElementById("share-save");
const shareRole = document.getElementById("share-role");
const shareStatus = document.getElementById("share-status");
const shareLinkWrap = document.getElementById("share-link-wrap");
const shareLinkInput = document.getElementById("share-link-input");
const shareLinkCopy = document.getElementById("share-link-copy");

function getCsrfToken() {
  const csrf = document.cookie.match(/(?:^|;\s*)gf_csrf=([^;]*)/);
  return csrf ? csrf[1] : "";
}

function buildShareUrl() {
  return `${location.origin}/w/${workspaceId}`;
}

function showLink(role) {
  if (role === "none") {
    shareLinkWrap.hidden = true;
    shareStatus.textContent = "Workspace is private. Only you can see it.";
  } else {
    shareLinkInput.value = buildShareUrl();
    shareLinkWrap.hidden = false;
    shareStatus.textContent =
      role === "editor"
        ? "Anyone with a GraphForge account can edit this workspace."
        : "Anyone with a GraphForge account can view this workspace.";
  }
}

async function openSharing() {
  shareStatus.textContent = "";
  shareLinkWrap.hidden = true;

  try {
    const response = await fetch(
      `/api/w/${workspaceId}/sharing`,
      {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": getCsrfToken() },
      }
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Could not load sharing settings.");
    }

    shareRole.value = data.role;
    showLink(data.role);
    shareModal.hidden = false;
  } catch (err) {
    alert(err.message);
  }
}

function closeSharing() {
  shareModal.hidden = true;
}

async function saveSharing() {
  shareSave.disabled = true;
  shareStatus.textContent = "Saving…";

  try {
    const response = await fetch(
      `/api/w/${workspaceId}/sharing`,
      {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": getCsrfToken(),
        },
        body: JSON.stringify({ role: shareRole.value }),
      }
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Could not save sharing settings.");
    }

    showLink(data.role);
  } catch (err) {
    shareStatus.textContent = err.message;
  } finally {
    shareSave.disabled = false;
  }
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(shareLinkInput.value);
    shareLinkCopy.textContent = "Copied!";
    setTimeout(() => { shareLinkCopy.textContent = "Copy"; }, 2000);
  } catch {
    shareLinkInput.select();
  }
}

shareBtn.addEventListener("click", openSharing);
shareClose.addEventListener("click", closeSharing);
shareCancel.addEventListener("click", closeSharing);
shareSave.addEventListener("click", saveSharing);
shareLinkCopy.addEventListener("click", copyLink);

shareModal.addEventListener("click", function (event) {
  if (event.target === shareModal) {
    closeSharing();
  }
});