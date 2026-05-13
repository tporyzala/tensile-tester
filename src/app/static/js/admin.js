(function () {
  const feedbackEl = document.getElementById("admin-command-feedback");

  async function postCommand(url) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    const body = await response.json().catch(() => ({}));
    if (!feedbackEl) {
      return;
    }
    if (!response.ok) {
      feedbackEl.innerHTML = `<div class="notice warning">${body.detail || "Request failed."}</div>`;
      return;
    }
    feedbackEl.innerHTML = `<div class="notice success">${body.message || "Command sent."}</div>`;
    window.location.reload();
  }

  document.querySelectorAll("[data-admin-action]").forEach((button) => {
    button.addEventListener("click", () => postCommand(button.dataset.adminAction));
  });
})();
