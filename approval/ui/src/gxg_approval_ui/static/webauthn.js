// WebAuthn for the passkey page and for step-up on a decision (AS-16, AS-17). No inline script (CSP, AS-15).
"use strict";

async function stepUp(form) {
  const options = await (await fetch(form.dataset.stepUp, { credentials: "same-origin" })).json();
  const credential = await navigator.credentials.get({
    publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
  });
  form.elements.assertion.value = JSON.stringify(credential.toJSON());
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("decide");
  if (form && form.dataset.stepUp) {
    form.addEventListener("submit", async (event) => {
      const button = event.submitter;
      if (!button || button.value !== "approve" || form.dataset.asked) return;
      event.preventDefault();
      try {
        await stepUp(form);
      } catch (e) {
        form.elements.assertion.value = "";
      }
      form.dataset.asked = "1"; // one attempt; the server answers 403 without a valid assertion
      form.requestSubmit(button);
    });
  }
  const register = document.getElementById("register");
  if (register) {
    register.addEventListener("click", async () => {
      const out = document.getElementById("register-result");
      try {
        const options = await (await fetch("/passkeys/options", { credentials: "same-origin" })).json();
        const credential = await navigator.credentials.create({
          publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
        });
        const response = await fetch("/passkeys", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": register.dataset.csrf },
          body: JSON.stringify(credential.toJSON()),
        });
        out.textContent = (await response.json()).registered ? "Passkey зарегистрирован" : "Не удалось";
      } catch (e) {
        out.textContent = "Не удалось";
      }
    });
  }
});
