// WebAuthn for the passkey page and for every decision (AS-16, AS-17, AS-30). No inline script (CSP, AS-15).
// The decider issues the challenge and checks the assertion; this page only asks the authenticator.
"use strict";

async function sign(form, decision) {
  const body = new FormData();
  body.set("decision", decision);
  body.set("version", form.elements.version.value);
  body.set("args_hash", form.elements.args_hash.value);
  const response = await fetch(form.dataset.challenge, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": form.elements.csrf.value },
    body,
  });
  const options = await response.json();
  const credential = await navigator.credentials.get({
    publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(options),
  });
  form.elements.assertion.value = JSON.stringify(credential.toJSON());
}

async function postJson(url, csrf, doc) {
  return fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(doc),
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("decide");
  if (form && form.dataset.challenge) {
    form.addEventListener("submit", async (event) => {
      const button = event.submitter;
      if (!button || form.dataset.asked) return;
      event.preventDefault();
      try {
        await sign(form, button.value);
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
      const invite = document.getElementById("invite").value.trim();
      try {
        const options = await (await postJson("/passkeys/options", register.dataset.csrf, { invite })).json();
        const credential = await navigator.credentials.create({
          publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
        });
        const response = await postJson("/passkeys", register.dataset.csrf, { invite, credential: credential.toJSON() });
        out.textContent = (await response.json()).registered ? "Passkey зарегистрирован" : "Не удалось";
      } catch (e) {
        out.textContent = "Не удалось";
      }
    });
  }
});
