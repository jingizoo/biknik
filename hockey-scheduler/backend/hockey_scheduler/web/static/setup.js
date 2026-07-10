(() => {
  "use strict";

  const form = document.getElementById("claim-form");
  const statusBox = document.getElementById("setup-status");
  const message = document.getElementById("setup-message");
  const signIn = document.getElementById("sign-in-link");
  const button = document.getElementById("claim-button");
  const codeInput = document.getElementById("setup-code");
  const usernameInput = document.getElementById("username");
  const passwordInput = document.getElementById("password");
  const confirmInput = document.getElementById("password-confirm");

  function unavailableText(reason) {
    if (reason === "already_claimed") {
      return "This installation has already been claimed. Sign in with an existing account.";
    }
    if (reason === "not_production") {
      return "Installation claim is available only on a production deployment.";
    }
    return "Initial setup is not available. Contact the deployment administrator.";
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (_error) {
      return {};
    }
  }

  async function loadStatus() {
    form.hidden = true;
    signIn.hidden = true;
    message.textContent = "";
    try {
      const response = await fetch("/api/bootstrap/status", {
        headers: {"Accept": "application/json"},
        cache: "no-store"
      });
      const payload = await readJson(response);
      if (response.ok && payload.claim_available === true) {
        statusBox.textContent = "This fresh installation is ready to be claimed.";
        form.hidden = false;
        usernameInput.focus();
        return;
      }
      statusBox.textContent = unavailableText(payload.reason);
      signIn.hidden = payload.reason !== "already_claimed";
    } catch (_error) {
      statusBox.textContent = "Initial setup status could not be checked. Contact the deployment administrator.";
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.className = "message";
    message.textContent = "";

    if (passwordInput.value !== confirmInput.value) {
      message.textContent = "Passwords do not match.";
      confirmInput.focus();
      return;
    }

    button.disabled = true;
    try {
      const response = await fetch("/api/bootstrap/claim", {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          setup_code: codeInput.value,
          username: usernameInput.value,
          password: passwordInput.value
        })
      });
      const payload = await readJson(response);

      // Secret fields are cleared after every attempt and never enter browser
      // storage, the URL, or a rendered error message.
      codeInput.value = "";
      passwordInput.value = "";
      confirmInput.value = "";

      if (response.ok) {
        message.className = "message success";
        message.textContent = "League Admin created. Redirecting to the application…";
        form.hidden = true;
        window.location.assign("/");
        return;
      }

      message.textContent = (payload.error && payload.error.message)
        || "The installation could not be claimed.";
      if (response.status === 409) {
        form.hidden = true;
        signIn.hidden = false;
      }
    } catch (_error) {
      codeInput.value = "";
      passwordInput.value = "";
      confirmInput.value = "";
      message.textContent = "The installation could not be claimed. Check the connection and try again.";
    } finally {
      button.disabled = false;
    }
  });

  loadStatus();
})();
