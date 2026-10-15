// Screenshot Dobby Console at the three target devices.
// Usage: node capture-dashboard.mjs [outDir]
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright-core";

const baseUrl = process.env.DASH_URL || "http://127.0.0.1:8765/";
const outDir = process.argv[2] || path.resolve("..", "..", "artifacts", "dashboard-screenshots");
const edgePath = ["C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find(p => fs.existsSync(p));

const VIEWPORTS = [
  { name: "phone", width: 420, height: 912, dsf: 2 },     // iPhone Air-ish
  { name: "laptop", width: 1512, height: 950, dsf: 2 },   // MacBook Pro 14"
  { name: "desktop", width: 2560, height: 1400, dsf: 1 }, // LG 32" 1440p
];
const TABS = ["overview", "assistant", "hermes", "code", "aihub", "studio", "media", "terminal", "system"];

fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ executablePath: edgePath, headless: true });
try {
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: vp.dsf,
    });
    const page = await ctx.newPage();
    await page.goto(baseUrl, { waitUntil: "networkidle" }).catch(() => {});
    await page.waitForTimeout(3500); // let stats/gauges populate

    for (const tab of TABS) {
      await page.click(`.nb[data-t="${tab}"]`).catch(() => {});
      await page.waitForTimeout(tab === "overview" ? 500 : 1200);
      if (tab === "code") {
        // open both details so the advanced/rollback UI is visible
        await page.evaluate(() => document.querySelectorAll("#code details").forEach(d => (d.open = true)));
        await page.waitForTimeout(200);
      }
      if (tab === "aihub") {
        await page.evaluate(() => { const m = document.getElementById("messaging-panel"); if (m) m.open = true; });
        await page.waitForTimeout(600);
      }
      if (tab === "system") {
        await page.click('.seg-btn[data-sys="cpu"]').catch(() => {});
        await page.waitForTimeout(700);
      }
      const file = path.join(outDir, `${vp.name}-${tab}.png`);
      await page.screenshot({ path: file, fullPage: tab !== "assistant" });
      console.log("shot", file);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log("DONE");
