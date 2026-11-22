// Playwright E2E of the Approval Web UI (AC-M9-01, AC-M9-05 in the DOM, AC-M9-06 cookie in the browser, step-up
// with a virtual WebAuthn authenticator). Driven by test_e2e.py, which owns the database, the ui process and the
// test OIDC issuer. `node e2e.mjs <step>`; input in E2E_* variables; one JSON object of results on stdout.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ui = process.env.E2E_UI_URL;
const state = process.env.E2E_STATE_FILE;
const ids = JSON.parse(process.env.E2E_INTENTS);

async function waitFor(url) {
  for (let i = 0; i < 150; i++) {
    try {
      if ((await fetch(url)).ok) return;
    } catch (e) {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`not up: ${url}`);
}

async function status(page, id) {
  await page.goto(`${ui}/intents/${id}`);
  return (await page.textContent("#status")).trim();
}

async function login(page, sub) {
  await page.goto(`${ui}/`);
  await page.click("#login");
  await page.fill("#subject-input", sub);
  await page.click("button:has-text('Authorize')");
  await page.waitForURL(`${ui}/`);
}

async function main(step) {
  await waitFor(`${ui}/healthz`);
  await waitFor(`${process.env.E2E_ISSUER}/.well-known/openid-configuration`);
  const browser = await chromium.launch();
  const out = {};
  try {
    if (step === "approve") {
      const context = await browser.newContext();
      const page = await context.newPage();
      const cdp = await context.newCDPSession(page);
      await cdp.send("WebAuthn.enable");
      await cdp.send("WebAuthn.addVirtualAuthenticator", {
        options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
                   isUserVerified: true, automaticPresenceSimulation: true },
      });
      await login(page, process.env.E2E_SUB);
      out.cookies = (await context.cookies()).map((c) => ({ name: c.name, httpOnly: c.httpOnly, secure: c.secure,
                                                          sameSite: c.sameSite, path: c.path, domain: c.domain }));
      out.listed = await page.locator(`a[href="/intents/${ids.plain}"]`).count();

      // AC-M9-05 in the DOM: no element made from values, markers, warning, full length
      await page.goto(`${ui}/intents/${ids.hostile}`);
      out.preview = await page.evaluate(() => {
        const section = document.getElementById("preview");
        const tags = [...section.querySelectorAll("*")].map((e) => e.tagName.toLowerCase());
        const values = [...section.querySelectorAll("pre.pv-value")].map((e) => e.textContent);
        return { tags: [...new Set(tags)], values, lengths: values.map((v) => v.length),
                 scripts: document.scripts.length, warning: !!document.getElementById("hidden-warning"),
                 alerted: window.__alerted === true };
      });

      // AC-M9-01: approve → approved
      out.before = await status(page, ids.plain);
      await page.click("#approve");
      await page.waitForURL(`${ui}/intents/${ids.plain}`);
      out.after = (await page.textContent("#status")).trim();

      // step-up: no passkey → no approve button; register a passkey; approve with the authenticator
      await page.goto(`${ui}/intents/${ids.wire}`);
      out.noPasskeyShown = await page.locator("#no-passkey").count();
      await page.goto(`${ui}/passkeys`);
      await page.click("#register");
      await page.waitForFunction(() => document.getElementById("register-result").textContent !== "");
      out.registered = await page.textContent("#register-result");
      out.wireBefore = await status(page, ids.wire);
      // the click runs navigator.credentials.get first, then submits: wait for the decision POST itself
      const [decision] = await Promise.all([
        page.waitForResponse((r) => r.url().endsWith("/decision") && r.request().method() === "POST"),
        page.click("#approve"),
      ]);
      out.wireDecisionStatus = decision.status();
      await page.waitForURL(`${ui}/intents/${ids.wire}`);
      out.wireAfter = (await page.textContent("#status")).trim();
      out.wireDecisions = await page.textContent("#decisions");
      await context.storageState({ path: state });
    } else if (step === "status") {
      const context = await browser.newContext({ storageState: state });
      const page = await context.newPage();
      out.final = await status(page, ids.plain);
    } else if (step === "requester") {
      // SEC-07 in the browser: the requester (same OIDC subject) is refused by the server
      const context = await browser.newContext();
      const page = await context.newPage();
      await login(page, process.env.E2E_SUB);
      await page.goto(`${ui}/intents/${ids.own}`);
      const [response] = await Promise.all([page.waitForResponse((r) => r.request().method() === "POST"),
                                            page.click("#approve")]);
      out.ownStatus = response.status();
      out.ownAfter = await status(page, ids.own);
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out));
}

main(process.argv[2]).catch((e) => {
  console.error(e);
  process.exit(1);
});
