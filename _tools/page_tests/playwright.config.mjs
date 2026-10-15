// Playwright Test config for the pages Anton builds (app/static/*.html).
// app/page_tests.py writes one spec per page into specs/ and runs copies of it
// from runs/<slug>-<stamp>/ (PAGE_TESTS_DIR), against the served page and
// against a blank and a scripts-removed copy of it. By hand:
//   cd _tools/page_tests
//   npx playwright test specs/<slug>.spec.mjs
//
// The browser is the Chromium already on disk (ms-playwright\chromium-*), the
// same one the jellyfin screenshot tools use: @playwright/test 1.61.1 wants
// headless-shell build 1228 and the disk has 1208, and fetching another
// ~250 MB for a version bump is not worth it. The service runs as LocalSystem,
// whose LOCALAPPDATA is not the user's, so USER_HOME's copy is listed too.
import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "@playwright/test";

function findChromium() {
  const roots = [
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, "ms-playwright") : "",
    process.env.USER_HOME ? path.join(process.env.USER_HOME, "AppData", "Local", "ms-playwright") : "",
  ].filter(Boolean);
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    const dirs = fs.readdirSync(root).filter((n) => /^chromium-\d+$/.test(n)).sort().reverse();
    for (const d of dirs) {
      const exe = path.join(root, d, "chrome-win", "chrome.exe");
      if (fs.existsSync(exe)) return exe;
      const exe64 = path.join(root, d, "chrome-win64", "chrome.exe");
      if (fs.existsSync(exe64)) return exe64;
    }
  }
  return undefined;
}

// the blank / scripts-removed runs only need to see a test FAIL, so they run
// with short waits; the real run keeps Playwright's normal patience
const ms = (name, dflt) => Number(process.env[name]) || dflt;

export default defineConfig({
  testDir: process.env.PAGE_TESTS_DIR || ".",
  testMatch: /.*\.spec\.mjs$/,
  // by hand from this folder: never pick up a leftover run copy
  testIgnore: process.env.PAGE_TESTS_DIR ? [] : ["**/runs/**", "**/node_modules/**"],
  timeout: ms("PAGE_TESTS_TEST_MS", 60_000),
  expect: { timeout: ms("PAGE_TESTS_EXPECT_MS", 5_000) },
  retries: 0,
  workers: 1,
  reporter: [["json", { outputFile: process.env.PAGE_TESTS_REPORT || "report.json" }], ["line"]],
  outputDir: process.env.PAGE_TESTS_OUT || "test-results",
  use: {
    headless: true,
    viewport: { width: 420, height: 912 },
    hasTouch: true,
    actionTimeout: ms("PAGE_TESTS_ACTION_MS", 10_000),
    navigationTimeout: 20_000,
    launchOptions: { executablePath: findChromium() },
  },
});
