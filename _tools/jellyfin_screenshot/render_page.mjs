import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

function findChromium() {
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
  for (const p of [
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  ]) if (fs.existsSync(p)) return p;
  return "";
}

const url = process.argv[2];
const waitMs = Number(process.argv[3] || 3500);
if (!url) { console.error("usage: node render_page.mjs <url> [waitMs]"); process.exit(2); }

const exe = findChromium();
if (!exe) { console.error("no chromium/edge/chrome binary found"); process.exit(3); }

const browser = await chromium.launch({ executablePath: exe, headless: true });
try {
  const ctx = await browser.newContext({
    locale: "lv-LV",
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  // listings arrive after hydration; networkidle often never fires on these
  // sites because of trackers, so wait a fixed beat instead
  await page.waitForTimeout(waitMs);
  const text = await page.evaluate(() => {
    for (const el of document.querySelectorAll("script,style,noscript")) el.remove();
    return (document.body ? document.body.innerText : "").replace(/\n{3,}/g, "\n\n");
  });
  process.stdout.write(text);
} finally {
  await browser.close();
}
