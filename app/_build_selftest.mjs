
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const URL_ = args[0];
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const CHROME = opt("--chrome",
  ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
   "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
   "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].find((p) => existsSync(p)));
const SETTLE = Math.max(1500, Math.min(parseInt(opt("--wait", "3500"), 10) || 3500, 9000));
const SHOTS_DIR = opt("--shots", "");
const PORT = 9310 + Math.floor(Math.random() * 600);

const done = (o) => { process.stdout.write(JSON.stringify(o)); process.exit(0); };
if (!URL_) done({ ok: false, error: "no url", passed: 0, total: 0 });
if (!CHROME) done({ ok: false, error: "no chrome/edge binary", passed: 0, total: 0 });

const profile = mkdtempSync(join(tmpdir(), "antons-cdp-selftest-"));
const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-sync",
  "--disable-extensions", "--disable-background-networking",
  "--window-position=-4000,-4000", "--window-size=430,860",
  "--mute-audio", "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
  "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
  "--disable-background-timer-throttling", "--disable-ipc-flooding-protection",
  "about:blank",
], { stdio: "ignore", detached: false });

const HARD = SETTLE + 140000;
const killTimer = setTimeout(() => finish({ ok: false, error: "hard timeout" }), HARD);
let finished = false;
function finish(o) {
  if (finished) return;
  finished = true;
  clearTimeout(killTimer);
  try { chrome.kill("SIGKILL"); } catch {}
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
  // scored items: smoke, the play-through, and each model check
  const playScored = o.play && !("error" in o.play) && !/no board/.test(o.play.ended || "");
  const playMoves = o.play ? (o.play.real_moves != null ? o.play.real_moves : o.play.moves) : 0;
  const playOk = playScored && playMoves >= 3
    && !(o.play.errors_during_play && o.play.errors_during_play.length);
  const total = (o.smoke ? 1 : 0) + (playScored ? 1 : 0) + (o.checks ? o.checks.length : 0);
  const passed = (o.smoke && o.smoke.pass ? 1 : 0) + (playOk ? 1 : 0)
    + (o.checks ? o.checks.filter((c) => c.pass).length : 0);
  done({ passed, total, ...o });
}
process.on("uncaughtException", (e) => finish({ ok: false, error: "uncaught: " + (e && e.message) }));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let msgId = 0;
const pending = new Map();
function cmd(ws, method, params = {}, timeoutMs = 15000) {
  const id = ++msgId;
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); rej(new Error(method + " timed out")); } }, timeoutMs);
  });
}

// evaluate an expression in the page, return the value (JSON round-trip)
async function ev(ws, expression, timeoutMs = 15000) {
  const r = await cmd(ws, "Runtime.evaluate", {
    expression, returnByValue: true, awaitPromise: true,
  }, timeoutMs);
  if (r && r.exceptionDetails) {
    throw new Error((r.exceptionDetails.exception && r.exceptionDetails.exception.description)
      || r.exceptionDetails.text || "eval threw");
  }
  return r && r.result ? r.result.value : undefined;
}

// The in-page routine that does the generic smoke test. Returns a summary.
const SMOKE_JS = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = { runtime_errors: [] };
  window.addEventListener("error", (e) => out.runtime_errors.push(String(e.message || e).slice(0,200)));
  window.addEventListener("unhandledrejection", (e) => out.runtime_errors.push("promise: " + String((e.reason && e.reason.message) || e.reason).slice(0,180)));

  const bodyText = (document.body ? document.body.innerText : "").trim();
  const svgN = document.querySelectorAll("svg").length;
  const canvasN = document.querySelectorAll("canvas").length;
  out.body_has_content = bodyText.length > 15 || canvasN > 0 || svgN >= 6;
  out.text_len = bodyText.length;

  // interactive candidates: real controls + repeated "board cell" style nodes
  const sel = 'button:not([disabled]),[role="button"],[onclick],a[href^="#"],' +
    '[class*="tile" i],[class*="cell" i],[class*="card" i],[class*="piece" i],' +
    '[data-i],[data-idx],[data-r],[data-c]';
  let cand = [...document.querySelectorAll(sel)].filter((e) => {
    const r = e.getBoundingClientRect();
    return r.width > 4 && r.height > 4 && e.offsetParent !== null;
  });
  out.interactive_found = cand.length;

  // detect a "board": >=16 elements that share a class token
  const tokCount = {};
  for (const e of cand) for (const t of e.classList) tokCount[t] = (tokCount[t] || 0) + 1;
  const boardTok = Object.keys(tokCount).find((t) => tokCount[t] >= 16);
  const allBoard = boardTok ? cand.filter((e) => e.classList.contains(boardTok)) : [];
  out.board_token = boardTok || "";
  out.board_size = allBoard.length;

  // Only cells a finger could ACTUALLY hit and that the game treats as live:
  // not disabled/locked, and the topmost element at its own centre (a covered
  // tile in a stacked mound is not playable, and tapping it proves nothing).
  const deadRe = /locked|disabled|removed|blocked|done|matched|gone|dimmed|inactive/i;
  const playable = allBoard.filter((e) => {
    if (e.hasAttribute("disabled") || e.getAttribute("aria-disabled") === "true") return false;
    if (deadRe.test(e.className)) return false;
    const r = e.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit && (hit === e || e.contains(hit));
  });
  // spread the picks across the board, not the first N (in a stacked game the
  // first N in DOM order are the bottom layer)
  const step = Math.max(1, Math.floor(playable.length / 8));
  const boardEls = [];
  for (let i = 0; i < playable.length && boardEls.length < 8; i += step) boardEls.push(playable[i]);
  out.board_playable = playable.length;

  let globalMutations = 0;
  const mo = new MutationObserver(() => { globalMutations++; });
  mo.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });

  function fp(el) {
    const cs = getComputedStyle(el);
    return [
      el.className, el.getAttribute("style") || "",
      el.getAttribute("aria-selected") || "", el.getAttribute("aria-pressed") || "",
      el.childElementCount, cs.boxShadow, cs.transform, cs.filter, cs.outlineStyle,
    ].join("|");
  }

  async function tap(el) {
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const base = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window };
    try { el.dispatchEvent(new PointerEvent("pointerover", { ...base, pointerId: 1, isPrimary: true, pointerType: "touch" })); } catch {}
    el.dispatchEvent(new PointerEvent("pointerdown", { ...base, pointerId: 1, isPrimary: true, pointerType: "touch", button: 0 }));
    el.dispatchEvent(new MouseEvent("mousedown", { ...base, button: 0 }));
    el.dispatchEvent(new PointerEvent("pointerup", { ...base, pointerId: 1, isPrimary: true, pointerType: "touch", button: 0 }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...base, button: 0 }));
    // A synthetic click on an <a href> (or a link wrapping the tapped node)
    // NAVIGATES Chrome off the page -- fine for a game button, fatal for a
    // launcher page that is nothing but links: every later check then runs on
    // whatever page we landed on. Fire pointer/mouse events so :active and JS
    // handlers still run, just skip the navigating click for real links.
    const lnk = el.closest && el.closest("a[href]");
    const href = lnk && lnk.getAttribute("href");
    const navigates = href && !/^(#|javascript:|mailto:|tel:)/i.test(href);
    if (!navigates) el.dispatchEvent(new MouseEvent("click", { ...base, button: 0 }));
    await sleep(90);
  }

  const skipRe = /new game|no jauna|restart|reset|delete|dzēst|clear|notīrīt|jauns|sākt no/i;
  const targets = [...boardEls];
  for (const e of cand) {
    if (targets.length >= 12) break;
    if (targets.includes(e) || allBoard.includes(e)) continue;
    if (skipRe.test((e.textContent || e.getAttribute("aria-label") || "").trim())) continue;
    targets.push(e);
  }

  out.taps_tried = 0;            // taps where SOMETHING on the page changed
  out.taps_self_reacted = 0;     // taps where the TAPPED element itself reacted
  out.board_taps = 0;
  out.board_taps_self_reacted = 0;
  for (const e of targets) {
    const isBoard = boardEls.includes(e);
    const gBefore = globalMutations;
    const selfBefore = fp(e);
    const rct = e.getBoundingClientRect();
    const px = rct.left + rct.width / 2, py = rct.top + rct.height / 2;
    const par = e.parentElement;
    const slot = par ? [...par.children].indexOf(e) : -1;
    await tap(e);
    let selfReacted = fp(e) !== selfBefore;
    if (!selfReacted) {
      const at = document.elementFromPoint(px, py);
      const atSlot = (par && slot >= 0) ? par.children[slot] : null;
      for (const c2 of [at, at && at.parentElement, atSlot]) {
        if (c2 && c2 !== e && fp(c2) !== selfBefore) { selfReacted = true; break; }
      }
    }
    const anyReacted = globalMutations > gBefore || selfReacted;
    out.taps_tried += anyReacted ? 1 : 0;
    if (selfReacted) out.taps_self_reacted++;
    if (isBoard) { out.board_taps++; if (selfReacted) out.board_taps_self_reacted++; }
  }

  out.board_driven = false;
  out.board_drive_method = "";
  {
    const cellSel = '[class*="tile" i],[class*="cell" i],[class*="piece" i],[class*="block" i],[class*="snake" i],[class*="ball" i],[class*="paddle" i]';
    const container = (allBoard[0] && allBoard[0].closest('[class*="board" i],[class*="grid" i],[id*="board" i],[id*="grid" i],canvas'))
      || (allBoard[0] && allBoard[0].parentElement) || document.querySelector("canvas") || document.body;
    const scoreEl = document.querySelector('[id="score"],[id*="score" i]:not([id*="box" i]):not([id*="board" i]),[class*="score" i]');
    const goneRe2 = /\\b(gone|removed|matched|cleared|solved|dead|collapsed|hidden)\\b/i;
    const _pos = (e) => e && /transform\s*:|(^|;)\s*left\s*:|(^|;)\s*top\s*:/i.test(e.getAttribute("style") || "");
    const _isCell = (e) => {
      const anc = e.parentElement && e.parentElement.closest(cellSel);
      if (anc && _pos(anc)) return false;
      return !e.querySelector(cellSel) || _pos(e);
    };
    const qc = () => [...document.querySelectorAll(cellSel)].filter(_isCell);
    const snap = () => {
      const t = qc();
      const vals = t.map((e) => (e.textContent || "").trim()).sort().join(",");
      const live = t.filter((e) => !goneRe2.test(e.className)).length;
      const sc = scoreEl ? ((scoreEl.textContent || "").match(/\\d+/) || [""])[0] : "";
      const xf = t.map((e) => ((e.getAttribute("style") || "").match(/(?:translate|left|top)[^;]*/gi) || []).join(",").replace(/-?[\\d.]+px/g, (m) => Math.round(parseFloat(m) / 40) * 40)).sort().join("|");
      return vals + "#" + t.length + "/" + live + "#" + sc + "#" + xf;
    };
    const changedFrom = async (before) => { await sleep(340); return snap() !== before; };
    const pressKey = (k, code) => {
      for (const t of ["keydown", "keyup"])
        for (const tgt of [document, window, document.body, container])
          try { tgt.dispatchEvent(new KeyboardEvent(t, { key: k, code, keyCode: code, which: code, bubbles: true, cancelable: true })); } catch {}
    };
    const doSwipe = async (dx, dy) => {
      const r = container.getBoundingClientRect();
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const P = (x, y) => ({ bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window, pointerId: 1, isPrimary: true, pointerType: "touch" });
      try {
        container.dispatchEvent(new PointerEvent("pointerdown", P(cx, cy)));
        container.dispatchEvent(new PointerEvent("pointermove", P(cx + dx * 0.5, cy + dy * 0.5)));
        container.dispatchEvent(new PointerEvent("pointermove", P(cx + dx, cy + dy)));
        container.dispatchEvent(new PointerEvent("pointerup", P(cx + dx, cy + dy)));
      } catch {}
      try {
        const T = (x, y) => (typeof Touch === "function"
          ? new Touch({ identifier: 1, target: container, clientX: x, clientY: y })
          : { identifier: 1, target: container, clientX: x, clientY: y });
        container.dispatchEvent(new TouchEvent("touchstart", { bubbles: true, cancelable: true, touches: [T(cx, cy)], targetTouches: [T(cx, cy)], changedTouches: [T(cx, cy)] }));
        container.dispatchEvent(new TouchEvent("touchmove", { bubbles: true, cancelable: true, touches: [T(cx + dx, cy + dy)], changedTouches: [T(cx + dx, cy + dy)] }));
        container.dispatchEvent(new TouchEvent("touchend", { bubbles: true, cancelable: true, touches: [], changedTouches: [T(cx + dx, cy + dy)] }));
      } catch {}
      await sleep(200);
    };
    const need = Math.max(2, Math.ceil(out.board_taps * 0.5));
    if (out.board_taps_self_reacted < need || out.board_size < 16) {
      await sleep(200);
      const before = snap();
      for (const [k, code] of [["ArrowLeft", 37], ["ArrowUp", 38], ["ArrowRight", 39], ["ArrowDown", 40], ["w", 87], [" ", 32]]) {
        pressKey(k, code);
        if (await changedFrom(before)) { out.board_driven = true; out.board_drive_method = "key:" + (k === " " ? "space" : k); break; }
      }
      if (!out.board_driven) {
        for (const [dx, dy] of [[-110, 0], [0, -110], [110, 0]]) {
          await doSwipe(dx, dy);
          if (await changedFrom(before)) { out.board_driven = true; out.board_drive_method = "swipe"; break; }
        }
      }
    }
  }

  function stillRendered(el) {
    if (!document.body.contains(el)) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && parseFloat(cs.opacity || "1") > 0.2;
  }
  const removedRe = /\b(gone|removed|matched|cleared|solved|vanish|dead|collapsed)\b/i;
  out.board_match_attempted = false;
  out.board_match_cleared = null;         // true = tiles vanished, false = stuck
  if (allBoard.length >= 16) {
    const leftBefore = (document.querySelector('[id*="left" i],[id*="count" i],[class*="left" i],[class*="count" i],[class*="remain" i]') || {}).textContent || "";
    // press a hint / suggestion control to get a guaranteed-valid pair
    const hint = [...document.querySelectorAll('button,[role=button],[onclick]')]
      .find((b) => /hint|padoms|help|suggest|palīdz|ieteikums/i.test((b.textContent || b.getAttribute("aria-label") || "")));
    let pair = [];
    if (hint) {
      hint.click();
      await sleep(250);
      pair = [...document.querySelectorAll('[class*="hint" i],[class*="glow" i],[class*="suggest" i],[class*="pulse" i]')]
        .filter((e) => allBoard.includes(e)).slice(0, 2);
    }
    if (pair.length < 2) {
      // fall back: two playable cells that render an identical inner symbol
      const key = (e) => (e.querySelector("svg") ? e.querySelector("svg").innerHTML : e.textContent || "").replace(/\s+/g, "");
      const byKey = {};
      for (const e of playable) (byKey[key(e)] ||= []).push(e);
      const g = Object.values(byKey).find((a) => a.length >= 2);
      if (g) pair = g.slice(0, 2);
    }
    if (pair.length === 2) {
      out.board_match_attempted = true;
      await tap(pair[0]);
      await tap(pair[1]);
      await sleep(650);                   // allow the fade / removal animation
      const marked = pair.filter((e) => removedRe.test(e.className) || e.getAttribute("aria-hidden") === "true" || !document.body.contains(e));
      const vanished = pair.filter((e) => !stillRendered(e));
      const leftAfter = (document.querySelector('[id*="left" i],[id*="count" i],[class*="left" i],[class*="count" i],[class*="remain" i]') || {}).textContent || "";
      out.board_left_before = leftBefore.trim().slice(0, 12);
      out.board_left_after = leftAfter.trim().slice(0, 12);
      // the game acted (marked them / counter moved) but they are still on screen
      const gameActed = marked.length >= 1 || (leftBefore && leftAfter && leftBefore !== leftAfter);
      out.board_match_cleared = gameActed ? (vanished.length === 2) : null;
    }
  }
  mo.disconnect();

  out.layout_sane = null;
  out.layout_detail = "";
  {
    const boardBox = (allBoard.length ? allBoard[0].closest('[class*="board" i],[id*="board" i],[class*="grid" i]') : null)
      || document.querySelector('[class*="board" i],[id*="board" i]');
    const tsel = '[class*="tile" i]';
    const liveTiles = () => [...document.querySelectorAll(tsel)]
      .filter((e) => !e.querySelector(tsel) && (e.textContent || "").trim())
      .map((e) => { const r = e.getBoundingClientRect(); return { el: e, cx: r.left + r.width / 2, cy: r.top + r.height / 2, w: r.width, h: r.height }; });
    if (boardBox && liveTiles().length >= 2) {
      const bb = boardBox.getBoundingClientRect();
      const cell = Math.max(20, Math.min(bb.width, bb.height) / 4);
      if (out.board_driven) {
        for (const [k, c] of [["ArrowUp", 38], ["ArrowDown", 40], ["ArrowLeft", 37]])
          for (const tgt of [document, window]) { try { tgt.dispatchEvent(new KeyboardEvent("keydown", { key: k, code: k, keyCode: c, which: c, bubbles: true, cancelable: true })); } catch {} }
      }
      await sleep(80);
      const mid = liveTiles();
      await sleep(650);   // slides (~120ms) and pops (~200ms) are long done
      const settled = liveTiles();
      // match mid<->settled by element identity; a tile that JUMPED far after
      // its animation should have ended = it was rendered in the wrong place.
      let jumped = 0, farJump = 0;
      for (const s of settled) {
        const m = mid.find((x) => x.el === s.el);
        if (!m) continue;
        const d = Math.hypot(s.cx - m.cx, s.cy - m.cy);
        if (d > cell * 0.7) { jumped++; farJump = Math.max(farJump, Math.round(d)); }
      }
      const zero = settled.filter((x) => x.w < 4 || x.h < 4).length;
      let piled = 0;
      for (let i = 0; i < settled.length; i++) { let n = 0; for (let j = 0; j < settled.length; j++) if (Math.abs(settled[i].cx - settled[j].cx) < 8 && Math.abs(settled[i].cy - settled[j].cy) < 8) n++; piled = Math.max(piled, n); }
      const outside = settled.filter((x) => x.cx < bb.left - cell || x.cx > bb.right + cell || x.cy < bb.top - cell || x.cy > bb.bottom + cell).length;
      out.layout_probe = { n: settled.length, jumped, farJump, zero, piled, outside };
      const bad = [];
      if (zero >= Math.ceil(settled.length / 2)) bad.push(zero + "/" + settled.length + " tiles render at 0x0");
      else if (piled >= settled.length && settled.length >= 3) bad.push("every tile stacked on one point (cell size <= 0?)");
      if (outside >= 1) bad.push(outside + " tile(s) sit outside the board");
      if (jumped >= 1) bad.push(jumped + " tile(s) teleport ~" + farJump + "px AFTER the move animation ends (a spawn/pop that animates transform, fighting the translate() positioning)");
      out.layout_sane = bad.length === 0;
      out.layout_detail = bad.join("; ");
    }
  }

  const fatal = out.runtime_errors.length > 0;
  let responsive;
  if (out.board_size >= 16 && out.board_taps >= 3) {
    // A tap-driven board (mahjong, memory, minesweeper): every PLAYABLE cell we
    // tapped must visibly react on itself -- the exact "tiles don't activate"
    // failure. A swipe/key game (2048, Snake) legitimately reacts to none of
    // them, so board_driven (arrow keys / swipe moved the board) also counts.
    responsive = out.board_taps_self_reacted >= Math.max(2, Math.ceil(out.board_taps * 0.5))
      || out.board_driven;
  } else if (out.interactive_found === 0) {
    responsive = true;
  } else {
    responsive = out.taps_self_reacted >= 1
      || out.taps_tried >= Math.max(1, Math.ceil(targets.length * 0.3))
      || out.board_driven;
  }
  out.responsive = responsive;
  // a move the game accepted but did not visibly complete = broken (pieces
  // that "don't disappear"). null = we could not set up a move, not a failure.
  const moveOk = out.board_match_cleared !== false;
  out.pass = out.body_has_content && responsive && !fatal && moveOk
    && out.layout_sane !== false;
  return out;
})()
`;

const INPUT_HELPERS_JS = `
if (!window.__tap) {
  let __n = 0; const __wait4 = {};
  window.__selftestInputDone = (id) => { const f = __wait4[id]; if (f) { delete __wait4[id]; f(); } };
  const __send = (o) => new Promise((res) => {
    if (typeof window.__selftestInput !== "function") {
      const el = document.elementFromPoint(o.x, o.y);
      if (el && o.kind === "tap") for (const t of ["pointerdown","mousedown","pointerup","mouseup","click"])
        el.dispatchEvent(new (t.startsWith("pointer") ? PointerEvent : MouseEvent)(t, { bubbles: true, cancelable: true, clientX: o.x, clientY: o.y }));
      if (o.kind === "key") for (const t of ["keydown","keyup"])
        document.dispatchEvent(new KeyboardEvent(t, { key: o.key, bubbles: true, cancelable: true }));
      return res();
    }
    const id = ++__n; __wait4[id] = res;
    window.__selftestInput(JSON.stringify(Object.assign({ id }, o)));
    setTimeout(() => { if (__wait4[id]) { delete __wait4[id]; res(); } }, 4000);
  });
  const __pt = (t) => {
    const el = typeof t === "string" ? document.querySelector(t) : t;
    if (!el) throw new Error("nothing on the page matches " + t);
    try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };
  const __settle = (ms) => new Promise((r) => setTimeout(r, ms));
  window.__wait = (ms) => __settle(ms || 300);
  window.__tapAt = async (x, y) => { await __send({ kind: "tap", x: x, y: y }); await __settle(150); };
  window.__tap = async (t) => { const p = __pt(t); await window.__tapAt(p.x, p.y); };
  window.__key = async (key) => { await __send({ kind: "key", key: key }); await __settle(120); };
  window.__swipe = async (t, dx, dy) => { const p = __pt(t); await __send({ kind: "swipe", x: p.x, y: p.y, dx: dx || 0, dy: dy || 0 }); await __settle(200); };
}
`;

const _KEYCODES = { ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Enter: 13,
  " ": 32, Escape: 27, Backspace: 8, Tab: 9 };
const _KEYNAMES = { " ": "Space", Enter: "Enter", Escape: "Escape", Backspace: "Backspace", Tab: "Tab" };

// the node side of the helpers: one trusted input, then wake the page
async function realInput(ws, o) {
  const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    if (o.kind === "tap") {
      await cmd(ws, "Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: o.x, y: o.y }] });
      await sleepMs(40);
      await cmd(ws, "Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } else if (o.kind === "swipe") {
      await cmd(ws, "Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: o.x, y: o.y }] });
      for (let i = 1; i <= 6; i++) {
        await sleepMs(16);
        await cmd(ws, "Input.dispatchTouchEvent", { type: "touchMove",
          touchPoints: [{ x: o.x + (o.dx * i) / 6, y: o.y + (o.dy * i) / 6 }] });
      }
      await cmd(ws, "Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } else if (o.kind === "key") {
      const key = String(o.key || "");
      const printable = key.length === 1;
      const vk = _KEYCODES[key] || (printable ? key.toUpperCase().charCodeAt(0) : 0);
      const code = _KEYNAMES[key] || (key.startsWith("Arrow") ? key
        : printable && /[a-z]/i.test(key) ? "Key" + key.toUpperCase()
        : printable && /\d/.test(key) ? "Digit" + key : key);
      await cmd(ws, "Input.dispatchKeyEvent", { type: printable ? "keyDown" : "rawKeyDown", key, code,
        windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, text: printable ? key : undefined });
      await cmd(ws, "Input.dispatchKeyEvent", { type: "keyUp", key, code,
        windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    }
  } catch (e) { /* the page's own timeout releases the check */ }
  try {
    await cmd(ws, "Runtime.evaluate", { expression: "window.__selftestInputDone && window.__selftestInputDone(" + Number(o.id) + ")" });
  } catch (e) {}
}

const TAPS_JS = `
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  // an overlay half-way through its fade-out is not a bug: let finite
  // animations finish (an endless pulse never does -- those are skipped)
  try {
    const fin = (document.getAnimations ? document.getAnimations() : []).filter((a) => {
      try { return isFinite(a.effect.getComputedTiming().endTime); } catch (e) { return false; }
    });
    if (fin.length) await Promise.race([Promise.all(fin.map((a) => a.finished.catch(() => 0))), sleep(1500)]);
  } catch (e) {}
  await sleep(80);
  const shown = (el) => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity || "1"); return o; };
  const hider = (el) => { for (let e = el; e && e.nodeType === 1; e = e.parentElement) if (parseFloat(getComputedStyle(e).opacity || "1") < 0.05) return e; return null; };
  const tag = (e) => "<" + e.tagName.toLowerCase() + (e.id ? " id=" + e.id : "")
    + (typeof e.className === "string" && e.className.trim() ? " class=" + e.className.trim().split(/\\s+/).slice(0, 2).join(".") : "") + ">";
  const label = (e) => { const t = (e.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 30); return tag(e) + (t ? " «" + t + "»" : ""); };
  const W = innerWidth, H = innerHeight, found = {};
  let sampled = 0;
  for (let gy = 1; gy <= 11; gy++) for (let gx = 1; gx <= 5; gx++) {
    const stack = document.elementsFromPoint(Math.round(W * gx / 6), Math.round(H * gy / 12));
    if (!stack.length) continue;
    sampled++;
    const top = stack[0];
    if (shown(top) >= 0.05 || /^(INPUT|SELECT|TEXTAREA)$/.test(top.tagName)) continue;
    const under = stack.find((e) => e !== document.body && e !== document.documentElement
      && !e.contains(top) && !top.contains(e) && shown(e) >= 0.05);
    if (!under) continue;
    const k = label(top) + "|" + tag(under), h = hider(top);
    (found[k] = found[k] || { what: label(top), over: tag(under),
      via: h && h !== top ? tag(h) : "", points: 0 }).points++;
  }
  return { href: location.href, sampled, blocked: Object.values(found).sort((a, b) => b.points - a.points).slice(0, 3) };
})()
`;

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

  const loadErrors = [];
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      if (m.error) p.rej(new Error(m.error.message || "cdp error")); else p.res(m.result);
      return;
    }
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params && m.params.exceptionDetails;
      const s = d && ((d.exception && (d.exception.description || d.exception.value)) || d.text);
      if (s) loadErrors.push(String(s).split("\n")[0].slice(0, 200));
    }
    if (m.method === "Runtime.consoleAPICalled" && m.params && m.params.type === "error") {
      const s = (m.params.args || []).map((a) => a.value || a.description || "").join(" ").trim();
      if (s) loadErrors.push(s.slice(0, 200));
    }
    if (m.method === "Runtime.bindingCalled" && m.params && m.params.name === "__selftestInput") {
      let o = null;
      try { o = JSON.parse(m.params.payload); } catch {}
      if (o) realInput(ws, o);
    }
  };

  await cmd(ws, "Page.enable");
  await cmd(ws, "Runtime.enable");
  try { await cmd(ws, "Runtime.addBinding", { name: "__selftestInput" }); } catch {}
  try {
    await cmd(ws, "Emulation.setDeviceMetricsOverride",
      { width: 402, height: 820, deviceScaleFactor: 2, mobile: true });
    await cmd(ws, "Emulation.setTouchEmulationEnabled", { enabled: true });
  } catch {}

  await cmd(ws, "Page.navigate", { url: URL_ });
  await sleep(SETTLE);

  // read the model's declared checks before we start poking the page
  let declared = [];
  let declared_error = "";
  try {
    const raw = await ev(ws, `(() => { const s = document.getElementById("selftest");
      return s ? s.textContent : ""; })()`);
    if (raw && raw.trim()) {
      let parsed = null;
      const rt = raw.trim();
      const tries = [raw, rt.replace(/,\s*$/, "") + "]",
        rt.replace(/,?\s*$/, "") + "}]",
        "[" + rt.replace(/^\[/, "").replace(/,?\s*$/, "") + "]"];
      // last resort: the last object is half-written (missing a closing quote on
      // its js string). Walk back through the "}" positions and rebuild the
      // array from just the objects that close cleanly before there -- a broken
      // final object is dropped, the good ones still run.
      let bpos = rt.lastIndexOf("}");
      for (let guard = 0; bpos > 0 && guard < 8; guard++) {
        const head = rt.slice(0, bpos + 1).replace(/^\s*\[/, "").replace(/[\s,]*$/, "");
        if (head) tries.push("[" + head + "]");
        bpos = rt.lastIndexOf("}", bpos - 1);
      }
      for (const t of tries) {
        try { const p = JSON.parse(t); if (Array.isArray(p) && p.length) { parsed = p; break; } } catch {}
      }
      if (Array.isArray(parsed)) declared = parsed.filter((c) => c && c.name && c.js).slice(0, 8);
      else declared_error = "selftest block is not valid JSON";
    }
  } catch (e) { declared_error = "selftest read failed: " + (e && e.message); }

  // smoke
  let smoke;
  try { smoke = await ev(ws, SMOKE_JS, 30000); }
  catch (e) { smoke = { pass: false, error: "smoke threw: " + e.message, runtime_errors: [], taps_tried: 0 }; }

  // what a real tap would hit in the state the page is in now; null when the
  // probe could not run or a tap followed a link off the page under test
  const tapsHere = async () => {
    try {
      const r = await ev(ws, TAPS_JS, 8000);
      return r && String(r.href || "").split("#")[0] === URL_.split("#")[0] ? r : null;
    } catch { return null; }
  };
  smoke.taps = await tapsHere();

  const checks = [];
  for (const c of declared) {
    try {
      await cmd(ws, "Page.navigate", { url: URL_ });
      await sleep(Math.min(SETTLE, 2600));
      const wrapped = `(async () => {
        ${INPUT_HELPERS_JS}
        try {
          const __r = await (async function(){ ${c.js} })();
          return { pass: !!__r, detail: (typeof __r === "string" ? __r : "") };
        } catch (e) { return { pass: false, detail: String(e && e.message || e).slice(0,200) }; }
      })()`;
      const res = await ev(ws, wrapped);
      checks.push({ name: String(c.name).slice(0, 80), pass: !!(res && res.pass),
                    detail: (res && res.detail) || "", taps: await tapsHere() });
    } catch (e) {
      checks.push({ name: String(c.name).slice(0, 80), pass: false, detail: "runner: " + e.message });
    }
  }

  // ---- PLAY IT. Reload to a fresh board and actually make moves like a
  // player -- greedy: keep matching a visible playable pair. Proves the core
  // loop end to end (select -> match -> remove -> next), catches errors that
  // only fire after a few moves, and gives us screenshots to look at.
  const shots = [];
  async function shot(label) {
    if (!SHOTS_DIR) return;
    try {
      const r = await cmd(ws, "Page.captureScreenshot", { format: "png" }, 8000);
      if (r && r.data) {
        try { mkdirSync(SHOTS_DIR, { recursive: true }); } catch {}
        const p = join(SHOTS_DIR, label + ".png");
        writeFileSync(p, Buffer.from(r.data, "base64"));
        shots.push({ label, path: p });
      }
    } catch {}
  }

  let play = null;
  try {
    await cmd(ws, "Page.navigate", { url: URL_ });
    await sleep(Math.min(SETTLE, 2800));
    await shot("01-start");
    const errsBefore = loadErrors.length;
    const PLAY_JS = `
    (async () => {
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const out = { moves: 0, real_moves: 0, removed: 0, method: "", ended: "", left_start: null, left_end: null };
      const cellSel = '[class*="tile" i],[class*="cell" i],[class*="card" i],[class*="piece" i],[class*="block" i]';
      const _pos = (e) => e && /transform\s*:|(^|;)\s*left\s*:|(^|;)\s*top\s*:/i.test(e.getAttribute("style") || "");
      const _isCell = (e) => {
        const anc = e.parentElement && e.parentElement.closest(cellSel);
        if (anc && _pos(anc)) return false;
        return !e.querySelector(cellSel) || _pos(e);
      };
      const qCells = () => [...document.querySelectorAll(cellSel)].filter(_isCell);
      const board = qCells();
      if (board.length < 8 && !document.querySelector("canvas")) { out.ended = "no board"; return out; }
      const container = (board[0] && board[0].closest('[class*="board" i],[class*="grid" i],[id*="board" i],[id*="grid" i],canvas'))
        || (board[0] && board[0].parentElement) || document.querySelector("canvas") || document.body;
      const numEl = document.querySelector('[id="score"],[id*="score" i]:not([id*="box" i]):not([id*="board" i]),[id*="left" i],[id*="count" i],[id*="remain" i],[class*="score" i],[class*="left" i],[class*="remain" i]');
      const numVal = () => { const t = (numEl && numEl.textContent || "").match(/\\d+/); return t ? +t[0] : null; };
      out.left_start = numVal();
      const goneRe = /\\b(gone|removed|matched|cleared|solved|dead|collapsed|hidden)\\b/i;
      const fp = () => {
        const t = qCells();
        const vals = t.map((e) => (e.textContent || "").trim()).sort().join(",");
        const live = t.filter((e) => !goneRe.test(e.className)).length;
        const xf = t.map((e) => ((e.getAttribute("style") || "").match(/(?:translate|left|top)[^;]*/gi) || []).join(",").replace(/-?[\\d.]+px/g, (m) => Math.round(parseFloat(m) / 40) * 40)).sort().join("|");
        return vals + "#" + t.length + "/" + live + "#" + (numVal() == null ? "" : numVal()) + "#" + xf;
      };
      const settledChange = async (before) => { await sleep(320); return fp() !== before; };
      const deadRe = /locked|disabled|removed|gone|matched|done|cleared|dimmed/i;
      const symKey = (e) => { const s = e.querySelector("svg"); return s ? (s.innerHTML.length + "|" + (s.textContent || "")) : (e.textContent || "").trim(); };
      function playable() {
        return qCells().filter((e) => {
          if (deadRe.test(e.className) || e.hasAttribute("disabled")) return false;
          const r = e.getBoundingClientRect();
          if (r.width < 4) return false;
          const h = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
          return h && (h === e || e.contains(h));
        });
      }
      function tap(e) {
        const r = e.getBoundingClientRect(), x = r.left + r.width/2, y = r.top + r.height/2;
        for (const t of ["pointerdown","mousedown","pointerup","mouseup","click"]) {
          const E = t.startsWith("pointer") ? PointerEvent : MouseEvent;
          e.dispatchEvent(new E(t, { bubbles:true, cancelable:true, clientX:x, clientY:y, pointerId:1, isPrimary:true }));
        }
      }
      const pressKey = (k, code) => {
        for (const t of ["keydown","keyup"])
          for (const tgt of [document, window, document.body, container])
            try { tgt.dispatchEvent(new KeyboardEvent(t, { key:k, code, keyCode:code, which:code, bubbles:true, cancelable:true })); } catch {}
      };
      async function swipe(dx, dy) {
        const r = container.getBoundingClientRect(), cx = r.left + r.width/2, cy = r.top + r.height/2;
        const P = (x,y) => ({ bubbles:true, cancelable:true, clientX:x, clientY:y, view:window, pointerId:1, isPrimary:true, pointerType:"touch" });
        try {
          container.dispatchEvent(new PointerEvent("pointerdown", P(cx,cy)));
          container.dispatchEvent(new PointerEvent("pointermove", P(cx+dx,cy+dy)));
          container.dispatchEvent(new PointerEvent("pointerup", P(cx+dx,cy+dy)));
        } catch {}
        try {
          const T = (x,y) => (typeof Touch === "function" ? new Touch({identifier:1,target:container,clientX:x,clientY:y}) : {identifier:1,target:container,clientX:x,clientY:y});
          container.dispatchEvent(new TouchEvent("touchstart", { bubbles:true, cancelable:true, touches:[T(cx,cy)], targetTouches:[T(cx,cy)], changedTouches:[T(cx,cy)] }));
          container.dispatchEvent(new TouchEvent("touchmove", { bubbles:true, cancelable:true, touches:[T(cx+dx,cy+dy)], changedTouches:[T(cx+dx,cy+dy)] }));
          container.dispatchEvent(new TouchEvent("touchend", { bubbles:true, cancelable:true, touches:[], changedTouches:[T(cx+dx,cy+dy)] }));
        } catch {}
      }
      const dirs = [["ArrowLeft",37,-110,0],["ArrowUp",38,0,-110],["ArrowRight",39,110,0],["ArrowDown",40,0,110]];
      // each returns the input name after firing it; the caller checks settledChange
      function actTap(i){
        const pl = playable();
        const byK = {};
        for (const e of pl) (byK[symKey(e)] = byK[symKey(e)] || []).push(e);
        const pk = Object.keys(byK).find((k) => byK[k].length >= 2);
        if (pk) { tap(byK[pk][0]); tap(byK[pk][1]); return "tap"; }
        if (pl.length) { tap(pl[Math.min(i, pl.length-1)]); return "tap"; }
        return null;
      }
      function actKey(i){ const d = dirs[i % 4]; pressKey(d[0], d[1]); return "key"; }
      async function actSwipe(i){ const d = dirs[i % 4]; await swipe(d[2], d[3]); return "swipe"; }
      const byName = { tap: actTap, key: actKey, swipe: actSwipe };
      const tileNums = () => qCells()
        .map((e) => parseInt((e.textContent || "").trim(), 10)).filter((n) => n > 0);
      const maxNum = () => tileNums().reduce((a, b) => Math.max(a, b), 0);
      out.start_max = maxNum();
      out.peak_max = out.start_max;
      out.peak_score = numVal() || 0;
      let stale = 0;
      for (let i = 0; i < 14; i++) {
        const ov = document.querySelector('[class*="overlay" i]:not([hidden]),[class*="modal" i]:not([hidden]),[id*="msg" i].show');
        if (ov && ov.offsetParent !== null && getComputedStyle(ov).opacity !== "0"
            && /uzvar|\\bwon\\b|\\bwin\\b|congrat|game ?over|zaud[eē]j|beig|beidz|pārsp|explod|sprādz|you lose|try again/i.test(ov.textContent || "")) {
          out.ended = "END:" + (ov.textContent || "").trim().replace(/\\s+/g," ").slice(0, 40); break;
        }
        await sleep(40);
        const b = fp();
        let acted = false;
        // once we know which input drives this game, use ONLY that. Otherwise
        // sweep tap -> key -> swipe (only for the first few moves).
        const tries = (out.method && byName[out.method]) ? [byName[out.method]]
          : (i < 3 ? [actTap, actKey, actSwipe] : [actTap]);
        for (const fn of tries) {
          const m = await fn(i);
          if (m == null) continue;
          if (await settledChange(b)) { acted = true; out.method = out.method || m; break; }
        }
        out.moves++;
        if (acted) { out.real_moves++; stale = 0; } else { stale++; }
        const n = numVal();
        if (n != null && out.left_start != null && n < out.left_start) out.removed = out.left_start - n;
        if (maxNum() > out.peak_max) out.peak_max = maxNum();
        if ((numVal() || 0) > out.peak_score) out.peak_score = numVal() || 0;
        if (stale >= 3) { out.ended = out.ended || ("no effect from tap/key/swipe after " + out.real_moves + " real move(s)"); break; }
      }
      out.left_end = numVal();

      out.advanced = (out.peak_max > out.start_max)
        || (out.peak_score > (out.left_start || 0))
        || (out.removed > 0)
        || /END:/.test(out.ended || "");

      // ---- Directional correctness. Only for key / swipe games. Reset, then
      // push each way twice and check the tiles' mean position moved TOWARD the
      // pressed edge. left => mean x drops, right => rises, up => y drops,
      // down => y rises. A game where >=2 of the 4 are backwards or dead (while
      // at least one works) has its controls scrambled -- "responsive" but
      // unplayable.
      out.dir_correct = null;
      if (out.method === "key" || out.method === "swipe") {
        const centre = () => {
          const t = qCells().filter((e) => (e.textContent || "").trim());
          if (!t.length) return null;
          let sx = 0, sy = 0;
          for (const e of t) { const r = e.getBoundingClientRect(); sx += r.left + r.width / 2; sy += r.top + r.height / 2; }
          return { x: sx / t.length, y: sy / t.length };
        };
        const reset = async () => {
          const nb = [...document.querySelectorAll('button,[role=button],[onclick]')]
            .find((b) => /new game|jauna|restart|reset|no jauna/i.test((b.textContent || b.getAttribute("aria-label") || "")));
          if (nb) { nb.click(); await sleep(300); }
        };
        const push = out.method === "key"
          ? async (d) => { for (let n = 0; n < 3; n++) { pressKey(d[0], d[1]); await sleep(230); } }
          : async (d) => { for (let n = 0; n < 3; n++) { await swipe(d[2], d[3]); await sleep(230); } };
        // [name, key, keycode, dx, dy, axis, sign]  sign: which way mean-pos should go
        const D = [["left", "ArrowLeft", 37, -140, 0, "x", -1], ["right", "ArrowRight", 39, 140, 0, "x", 1],
                   ["up", "ArrowUp", 38, 0, -140, "y", -1], ["down", "ArrowDown", 40, 0, 140, "y", 1]];
        let ok = 0, moved = 0;
        out.dir_detail = {};
        for (const [nm, k, kc, dx, dy, axis, sign] of D) {
          await reset();
          const c0 = centre(); if (!c0) continue;
          await push([k, kc, dx, dy]);
          const c1 = centre(); if (!c1) continue;
          const delta = axis === "x" ? c1.x - c0.x : c1.y - c0.y;
          const off = axis === "x" ? Math.abs(c1.y - c0.y) : Math.abs(c1.x - c0.x);
          out.dir_detail[nm] = Math.round(delta) + (off > 40 ? ("/perp" + Math.round(off)) : "");
          if (Math.abs(delta) > 12 || off > 40) moved++;
          if (delta * sign > 12 && off < Math.abs(delta) + 20) ok++;
        }
        out.dir_moved = moved;
        out.dir_correct = ok;
      }

      if (!out.ended) out.ended = out.real_moves >= 3 ? "played " + out.real_moves + " (" + out.method + ")" : "stalled";
      return out;
    })()`;
    play = await ev(ws, PLAY_JS, 55000);
    await shot("02-after-play");
    play.errors_during_play = loadErrors.slice(errsBefore).slice(0, 5);
  } catch (e) {
    play = { error: "play threw: " + (e && e.message) };
  }

  let title = "";
  try { title = await ev(ws, "document.title || ''"); } catch {}

  ws.close();
  const hardLoadErrs = loadErrors.filter((e) => !/favicon|net::ERR.*favicon/i.test(e));
  const _playReal = play ? (play.real_moves != null ? play.real_moves : play.moves) : 0;
  const playBroken = play && (play.error
    || (_playReal === 0 && !/no board/.test(play.ended || ""))
    || (play.errors_during_play && play.errors_during_play.length));
  finish({
    ok: hardLoadErrs.length === 0 && (smoke ? smoke.body_has_content !== false : true) && !playBroken,
    url: URL_,
    title: String(title || "").slice(0, 120),
    load_errors: [...new Set(loadErrors)].slice(0, 8),
    smoke,
    checks,
    declared_error,
    play,
    screenshots: shots,
  });
})().catch((e) => finish({ ok: false, error: "run failed: " + (e && e.message) }));
