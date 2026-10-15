import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright-core";

const baseUrl = (process.env.JELLYFIN_URL || "http://127.0.0.1:8096").replace(/\/$/, "");
const username = process.env.JELLYFIN_USER || "";
const password = process.env.JELLYFIN_PASSWORD || "";
const outputDir = process.env.JELLYFIN_SCREENSHOT_OUT || path.resolve("..", "..", "artifacts", "jellyfin-screenshots");
const storageState = process.env.JELLYFIN_STORAGE_STATE || path.resolve("..", "..", ".jellyfin-auth", "storage-state.json");
const route = process.env.JELLYFIN_SCREENSHOT_ROUTE || "/web/#/home";
const browserPath = process.env.PLAYWRIGHT_CHROMIUM_PATH || findPlaywrightChromium() || process.env.EDGE_PATH || findEdge();

function findPlaywrightChromium() {
  const roots = [
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, "ms-playwright") : "",
    process.env.USER_HOME ? path.join(process.env.USER_HOME, "AppData", "Local", "ms-playwright") : "",
  ].filter(Boolean);
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    const dirs = fs.readdirSync(root)
      .filter(name => /^chromium-\d+$/.test(name))
      .sort()
      .reverse();
    for (const dir of dirs) {
      const exe = path.join(root, dir, "chrome-win64", "chrome.exe");
      if (fs.existsSync(exe)) return exe;
    }
  }
  return "";
}

function findEdge() {
  const candidates = [
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return "";
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "").replace("T", "-");
}

async function fillFirst(page, selectors, value) {
  for (const selector of selectors) {
    const loc = page.locator(selector).first();
    if (await loc.count()) {
      await loc.fill(value);
      return true;
    }
  }
  return false;
}

async function signInIfNeeded(page) {
  await page.goto(baseUrl + "/web/#/login", { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  if (!/login/i.test(page.url())) return false;
  if (!username || !password) {
    throw new Error("Jellyfin login is required. Run once with JELLYFIN_USER and JELLYFIN_PASSWORD to create storage-state.json.");
  }
  const userOk = await fillFirst(page, [
    "input[autocomplete='username']",
    "input[name='Username']",
    "input[type='text']",
    "input[placeholder='User']"
  ], username);
  const passOk = await fillFirst(page, [
    "input[autocomplete='current-password']",
    "input[name='Password']",
    "input[type='password']",
    "input[placeholder='Password']"
  ], password);
  if (!userOk || !passOk) throw new Error("Could not find Jellyfin login fields.");
  await Promise.all([
    page.waitForURL(url => !/login/i.test(url.toString()), { timeout: 30000 }).catch(() => {}),
    page.getByRole("button", { name: "Sign In" }).click()
  ]);
  if (/login/i.test(page.url())) throw new Error("Jellyfin sign-in did not leave the login page.");
  ensureDir(path.dirname(storageState));
  await page.context().storageState({ path: storageState });
  return true;
}

async function captureOne(browser, size, label, loggedIn) {
  const contextOptions = {
    viewport: size,
    deviceScaleFactor: 1,
    isMobile: label === "mobile",
    hasTouch: label === "mobile"
  };
  if (loggedIn && fs.existsSync(storageState)) contextOptions.storageState = storageState;
  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", msg => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", err => consoleErrors.push(String(err && err.message || err)));
  const cacheBust = "anton_screenshot=" + Date.now();
  const targetUrl = baseUrl + (route.startsWith("/") ? route : "/" + route);
  const url = targetUrl.includes("?")
    ? targetUrl.replace("?", "?" + cacheBust + "&")
    : targetUrl.replace("/web/", "/web/?" + cacheBust);
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
  if (/login/i.test(page.url())) {
    await context.close();
    throw new Error("Stored Jellyfin auth is missing or expired.");
  }
  await page.waitForTimeout(2500);
  const shotPath = path.join(outputDir, `${stamp()}-${label}.png`);
  await page.screenshot({ path: shotPath, fullPage: true });
  const details = await page.evaluate(() => {
    const text = (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim();
    return {
      url: location.href,
      title: document.title,
      hasLumaWordmark: !!document.querySelector(".luma-wordmark"),
      hasLumaShell: document.documentElement.classList.contains("luma-shell"),
      hasHomeHero: !!document.querySelector(".luma-home-hero"),
      textSample: text.slice(0, 500)
    };
  });
  await context.close();
  return { label, size, screenshot: shotPath, consoleErrors, details };
}

async function main() {
  if (!browserPath) throw new Error("No Chromium or Microsoft Edge browser executable was found.");
  ensureDir(outputDir);
  const tmpDir = path.join(outputDir, ".browser-tmp");
  ensureDir(tmpDir);
  process.env.TEMP = tmpDir;
  process.env.TMP = tmpDir;
  const browser = await chromium.launch({
    executablePath: browserPath,
    headless: true,
    args: [
      "--disable-background-networking",
      "--disable-dev-shm-usage",
      "--disable-extensions",
      "--disable-gpu",
      "--no-first-run",
      "--no-sandbox"
    ]
  });

  let signedIn = fs.existsSync(storageState);
  if (!signedIn || username || password) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const didLogin = await signInIfNeeded(page);
    signedIn = signedIn || didLogin || fs.existsSync(storageState);
    await context.close();
  }

  const captures = [];
  captures.push(await captureOne(browser, { width: 390, height: 844 }, "mobile", signedIn));
  captures.push(await captureOne(browser, { width: 1280, height: 720 }, "desktop", signedIn));
  await browser.close();

  const report = {
    ok: true,
    baseUrl,
    browserPath,
    storageState,
    outputDir,
    captures
  };
  const reportPath = path.join(outputDir, `${stamp()}-report.json`);
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n", "utf8");
  report.report = reportPath;
  console.log(JSON.stringify(report));
}

main().catch(err => {
  console.error(err && err.stack || err);
  process.exit(1);
});
