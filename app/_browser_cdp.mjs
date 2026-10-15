
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const URL_ = args[0];
const opt = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : def;
};
const CHROME = opt("--chrome",
  ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
   "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
   "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"]
    .find((p) => existsSync(p)));
const SETTLE = parseInt(opt("--wait", "7000"), 10);
const DO_CLICK = !args.includes("--no-click");
const PORT = 9210 + Math.floor(Math.random() * 700);

const done = (obj) => { process.stdout.write(JSON.stringify(obj)); process.exit(0); };
if (!URL_) done({ ok: false, error: "no url" });
if (!CHROME) done({ ok: false, error: "no chrome/edge binary found" });

const profile = mkdtempSync(join(tmpdir(), "antons-cdp-"));
const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-sync",
  "--disable-extensions", "--disable-background-networking",
  "--window-position=-4000,-4000", "--window-size=1360,940",
  "--mute-audio", "--disable-features=Translate,MediaRouter",
  "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
  "--disable-background-timer-throttling",
  "about:blank",
], { stdio: "ignore", detached: false });

const HARD_TIMEOUT = SETTLE + 25000;
const killTimer = setTimeout(() => finish({ ok: false, error: "hard timeout" }), HARD_TIMEOUT);

let finished = false;
function finish(obj) {
  if (finished) return;
  finished = true;
  clearTimeout(killTimer);
  try { chrome.kill("SIGKILL"); } catch {}
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
  done(obj);
}
process.on("uncaughtException", (e) => finish({ ok: false, error: "uncaught: " + (e && e.message) }));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let msgId = 0;
const pending = new Map();
function cmd(ws, method, params = {}) {
  const id = ++msgId;
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); rej(new Error(method + " timed out")); } }, 15000);
  });
}

const API_RE = /\/api\/|\/graphql|\/gql\b|\/v[0-9]+\/|\.json($|\?)|priceCalendar|price-calendar|availab|schedule|timetable|results?\b/i;
// third-party analytics / consent / tag managers that fetch big JSON blobs of
// their own -- never the page's actual data
const NOISE_RE = /cookielaw|onetrust|go-mpulse|mpulse|adn\.cloud|doubleclick|googletag|google-?analytics|segment\.io|hotjar|optimizely|newrelic|nr-data|sentry|datadog|cdn\.cookie|cookieconsent|geolocation\./i;
const CLICK_RE = /(see|view|show|check)\s+(price|tarif|availab|calendar|dates|fares|schedule)|price\s+calendar|show\s+(schedule|timetable|results|more)|load\s+more|view\s+all/i;
const CLICK_SKIP = /login|sign\s*in|account|cookie|accept|consent|newsletter|subscribe|\bmenu\b|search|book\s*now|buy\b|add to (cart|basket)|checkout|pay\b|register|create account/i;

(async () => {
  let list;
  for (let i = 0; i < 60; i++) {
    try { list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json(); if (list && list.length) break; } catch {}
    await sleep(200);
  }
  const page = (list || []).find((t) => t.type === "page");
  if (!page) return finish({ ok: false, error: "no page target" });

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = () => j(new Error("ws error")); });

  const respUrl = new Map();
  const respType = new Map();
  const api = [];
  let lastNet = Date.now();

  ws.onmessage = async (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      if (m.error) p.rej(new Error(m.error.message || "cdp error"));
      else p.res(m.result);
      return;
    }
    if (m.method === "Network.requestWillBeSent") lastNet = Date.now();
    if (m.method === "Network.responseReceived") {
      respUrl.set(m.params.requestId, m.params.response.url);
      respType.set(m.params.requestId, (m.params.response.headers &&
        (m.params.response.headers["content-type"] || m.params.response.headers["Content-Type"])) || "");
      lastNet = Date.now();
    }
    if (m.method === "Network.loadingFinished") {
      lastNet = Date.now();
      const u = respUrl.get(m.params.requestId) || "";
      const ct = respType.get(m.params.requestId) || "";
      if (api.length < 20 && (API_RE.test(u) || /json/i.test(ct))
          && !NOISE_RE.test(u)
          && !/\.(png|jpe?g|webp|gif|svg|css|js|mjs|woff2?|map|ico)($|\?)/i.test(u)) {
        try {
          const b = await cmd(ws, "Network.getResponseBody", { requestId: m.params.requestId });
          let body = b.body || "";
          if (b.base64Encoded) { try { body = Buffer.from(body, "base64").toString("utf8"); } catch {} }
          body = body.trim();
          if (body && (body[0] === "{" || body[0] === "[")) api.push({ url: u, body: body.slice(0, 120000) });
        } catch {}
      }
    }
  };

  await cmd(ws, "Network.enable");
  await cmd(ws, "Page.enable");
  await cmd(ws, "Runtime.enable");
  try { await cmd(ws, "Emulation.setDeviceMetricsOverride", { width: 1360, height: 940, deviceScaleFactor: 1, mobile: false }); } catch {}
  await cmd(ws, "Page.navigate", { url: URL_ });

  // wait for network to go quiet, capped
  const settleDeadline = Date.now() + SETTLE + 4000;
  while (Date.now() < settleDeadline && Date.now() - lastNet < 1500) await sleep(200);
  await sleep(Math.min(SETTLE, 4000));

  let clicked = "";
  const wantClick = opt("--click", "");
  if (DO_CLICK || wantClick) {
    try {
      const r = await cmd(ws, "Runtime.evaluate", {
        expression: `(() => {
          const want = ${JSON.stringify(wantClick)};
          const rx = want ? new RegExp(want.replace(/[.*+?^\${}()|[\\]\\\\]/g,'\\\\$&'),'i')
                          : ${CLICK_RE.toString()};
          const skip = ${CLICK_SKIP.toString()};
          const els = [...document.querySelectorAll('button, a, [role=button], [role=tab], summary')];
          for (const e of els) {
            const t = (e.textContent||e.getAttribute('aria-label')||'').trim();
            if (t && t.length < 48 && rx.test(t) && (want || !skip.test(t)) && e.offsetParent !== null) {
              e.scrollIntoView(); e.click(); return t.slice(0,60);
            }
          }
          return '';
        })()`, returnByValue: true,
      });
      clicked = (r && r.result && r.result.value) || "";
    } catch {}
    if (clicked) {
      const d2 = Date.now() + 6000;
      while (Date.now() < d2 && Date.now() - lastNet < 1500) await sleep(200);
      await sleep(1500);
    }
  }

  let dom = {};
  try {
    const r = await cmd(ws, "Runtime.evaluate", {
      expression: `JSON.stringify({
        title: document.title || '',
        url: location.href,
        text: (document.body ? document.body.innerText : '').replace(/\\n{3,}/g,'\\n\\n').slice(0, 40000),
        blocked: /Access Denied|Reference #\\S|Just a moment|Attention Required|verify you are human/i.test(document.body ? document.body.innerText : ''),
        links: (() => {
          const host = location.hostname.replace(/^www\\./,'');
          const seen = new Set(), out = [];
          for (const a of document.querySelectorAll('a[href]')) {
            let u; try { u = new URL(a.href, location.href); } catch { continue; }
            if (!/^https?:/.test(u.protocol)) continue;
            const h = u.hostname.replace(/^www\\./,'');
            if (h !== host && !h.endsWith('.'+host)) continue;   // same site incl. subdomains
            const key = u.origin + u.pathname;
            if (seen.has(key)) continue; seen.add(key);
            const t = (a.textContent||'').trim().replace(/\\s+/g,' ').slice(0,80);
            if (t) out.push({ url: key, text: t });
            if (out.length >= 120) break;
          }
          return out;
        })()
      })`, returnByValue: true,
    });
    dom = JSON.parse(r.result.value);
  } catch (e) { dom = { error: "dom read failed: " + e.message }; }

  // dedupe api by url, keep the largest body per url
  const byUrl = new Map();
  for (const a of api) {
    const prev = byUrl.get(a.url);
    if (!prev || a.body.length > prev.body.length) byUrl.set(a.url, a);
  }

  ws.close();
  finish({
    ok: !dom.error && !dom.blocked,
    blocked: !!dom.blocked,
    title: dom.title || "",
    final_url: dom.url || URL_,
    text: dom.text || "",
    clicked,
    api: [...byUrl.values()].slice(0, 12),
    links: dom.links || [],
    error: dom.error || "",
  });
})().catch((e) => finish({ ok: false, error: "run failed: " + (e && e.message) }));
