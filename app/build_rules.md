# Build Rules

## base
You write ONE complete, self-contained HTML file and nothing else. Output ONLY the file: start with `<!doctype html>` and end with `</html>`. No prose before or after, no markdown fence, no explanation. If the file is long and you reach the length limit, just STOP mid-line -- you will be asked to continue from exactly there. Never restart the file, never summarise, never write '... rest of code ...'.
Rules:
- One file. All CSS in a single <style>, all JS in a single <script>. No build step, no imports, no npm, no external URL, no CDN, no framework, no web fonts, no external images -- inline anything you need (SVG, a data: URI). Plain HTML/CSS/JS only.
- Served at http://127.0.0.1:8765/static/<name>.html.
- Fills the screen, NO page scroll, works portrait and landscape, on a phone. Fixed non-scrolling body. Handle touch as well as mouse; if the game is directional (a 2048/snake/tetris style), ALSO bind the arrow keys and WASD, and add small on-screen direction buttons so it is playable on a phone with no keyboard.
- Never throw on missing data; guard every access.
- If pieces are positioned with `transform: translate(x,y)`, DO NOT also animate `transform` (a `@keyframes pop{from{transform:scale(0)}}` on the same element wins over the inline translate, so the piece appears at 0,0 and jumps). Put the scale/pop on a child element, or bake it in: `transform: translate(x,y) scale(s)`.
- Any layout math from a measured size (`clientWidth`, `getBoundingClientRect`) must guard `<= 0` (element not laid out yet) -- clamp the result to a minimum, and recompute on `resize`/`orientationchange` AND once after first paint (`requestAnimationFrame`).

## game
GAME LOGIC -- get the core rule EXACTLY right; a game that looks right but plays wrong is a failure.
- Keep ONE source of truth for the board (a 2D array or a list of piece objects with row/col). The DOM is a VIEW of it -- render from the model after every change, never read game state back out of pixels.
- Slide / merge (2048 and similar): for the pressed direction, walk each row or column FROM the target edge inward. Compact the non-empty cells against that edge; then merge two equal ADJACENT values into one (doubled) -- each tile merges at most ONCE per move; a tile that was just formed by a merge cannot merge again this move. `left` packs toward column 0, `right` toward the last column, `up` toward row 0, `down` toward the last row -- test all four, they are not the same code path with a flipped sign.
- A move counts if ANY piece changed cell OR any merge happened -- a plain slide into empty space with no merge is still a move. Decide this by comparing each piece's cell (or the whole board) BEFORE vs AFTER the move; snapshot it BEFORE you shift anything, never after. A move that truly changed nothing is a no-op: do not spawn, do not add score, do not switch turns.
- Spawn / deal a new piece ONLY after a move that actually changed the board, into a genuinely empty cell.
- Score: keep a running total variable, add to it on the scoring event, and write it to the DOM. A floating "+N" popup is decoration, not the score.
- New Game / Restart must reset EVERYTHING: the board model, the score number and its DOM, the win/lose/over flags, any overlay. Then deal the start position.
- Win and lose must both be reachable and must actually show (an overlay with the right text). "no moves left" = board full AND no adjacent equal pair.
- After you write `move()` / the core step, trace TWO examples in your head: (1) two `2`s in one row, press Left -> a single `4` at column 0, a new tile appears, score += 4; (2) ONE `2` alone at column 3, press Left -> that same `2` sits at column 0 and a new tile appears. If example (2) does nothing, your move-detection only sees merges -- fix it.

## data
LIVE RIG DATA (same origin):
- GET /api/stats every 2s. Fields (numeric unless noted): gpu.temperature_c, gpu.gpu_utilization_percent, gpu.memory_used_mb, gpu.memory_total_mb, gpu.power_draw_w, gpu.power_limit_w, gpu.fan_speed_percent, gpu.name (text); cpu.per_core_percent (array), cpu.frequency_mhz, cpu.package_temperature_c (often null), cpu_percent; memory.percent, memory.used_gb, memory.total_gb, memory.swap.percent; disks (array of {letter, percent, free_gb}); warnings (array of strings); uptime_seconds. `warnings` almost always has a chronic 'Disk M:: 95%' line and VRAM sits ~85% whenever a model is loaded -- neither is a real problem; do NOT alarm on disk-space or memory/VRAM warnings.
- GET /api/ollama/models returns {loaded:[{name,size_vram,expires_at}]}. A model whose name starts 'qwen' with size_vram>0 = the assistant is loaded/ready; only 'embed' or nothing = idle/asleep.

## selftest
SELF-TEST BLOCK (required). Just before </body>, add:
  <script type="application/json" id="selftest">[ ... ]</script>
a JSON array of 4-7 checks, each {"name": "...", "js": "..."}. `js` is the BODY of an async function (no wrapper; you may use `await`) that acts on the page and RETURNS true when the check passes, false otherwise. Each check runs on its OWN freshly loaded page, so a check must set up everything it needs itself and never rely on state another check made: a check that upgrades or sells something builds that thing first, in the same `js`. Keep each `js` one line, no backticks, no comments.

TOUCH THE PAGE THE WAY A PERSON DOES. The test browser (phone-sized, touch) gives every check these, and each one is a REAL finger or key -- it lands on whatever is on top at that spot, exactly as on a phone:
- `await __tap('#startBtn')` -- taps the middle of that element (a selector or an element)
- `await __tapAt(x, y)` -- taps a point on the screen (for a canvas game: a spot on the canvas)
- `await __key('ArrowLeft')` -- presses a key ('ArrowLeft', 'ArrowUp', ' ', 'Enter', 'a' ...)
- `await __swipe('#board', -120, 0)` -- drags a finger from that element's middle by dx, dy
- `await __wait(400)` -- lets an animation or a timer run
Do the player's action with these, then read the result. NEVER call your own game functions (`launchBall()`, `move('left')`, `startGame()`) and never `.click()` or `dispatchEvent` yourself in a check: those skip everything a finger goes through, so such a check passes on a game nobody can play -- a start button left under an invisible overlay passed 7/7 that way. Reading state to assert on is fine (`game.score`, a cell's class, a number on screen).

Every check must EXERCISE the game and assert the RESULT, not just read the opening state. "score starts at 0" and "there are 16 cells" are nearly worthless -- they pass on a completely dead game. For each check: perform the real player action, then assert the thing that action should have changed HAS changed.
- The MAIN MOVE must be one check: start the game the way a person does (tap its start button), then make the real move -- tap a cell, press an arrow, swipe the board, tap the canvas -- and assert something changed. IF YOUR render() REBUILDS THE GRID (innerHTML = ...), an element you held before the tap is now detached -- assert on a fresh query (`document.querySelectorAll('.cell.revealed').length > 0`) or re-select the cell at the same row/col.
- DIRECTIONAL games (2048/snake/sokoban): one check must prove `ArrowLeft` moves tiles LEFT specifically -- read a tile's x/left before, `await __key('ArrowLeft')`, assert its x/left DECREASED (and separately that ArrowRight increases it, or ArrowUp decreases top). A game where the keys are wired to the wrong axis still "moves" -- that check must fail it.
- If there is a SCORE: make a scoring move and assert the score went UP from its previous value. A "+N" popup is not the score; read the score element's number.
- A win or game-over state is reachable / the end text appears.
- No control is dead: the reset/new-game button, every powerup, the direction buttons -- tap each one.
- Never assert on the exact wording of a text (the words may be translated after you write them); assert on state, counts, classes and numbers.
Example entries:
  {"name":"start, then arrow-left moves a tile","js":"await __tap('#startBtn');var b=[].map.call(document.querySelectorAll('.tile'),function(t){return t.style.left}).join(',');await __key('ArrowLeft');await __wait(300);return b!==[].map.call(document.querySelectorAll('.tile'),function(t){return t.style.left}).join(',');"},
  {"name":"tapping a free cell selects it","js":"await __tap('.cell:not(.locked)');return document.querySelectorAll('.cell.sel').length===1;"},
  {"name":"a tap on the canvas launches the ball","js":"await __tap('#startBtn');var r=document.querySelector('canvas').getBoundingClientRect();await __tapAt(r.left+r.width/2,r.top+r.height*0.7);await __wait(300);return !!window.game&&!window.game.ball.stuck;"}

## ios9
TARGET IS AN iPHONE 4S -- iOS 9 Safari, a 2015 WebKit. STRICT.
USE: ES5 only (var, function; NO arrow functions, NO let/const, NO template literals, NO async/await); XMLHttpRequest, NOT fetch(); float/position for layout, NOT flexbox or grid; hex or rgb()/rgba() colours (convert any HSL to rgb in JS -- `hsl(H S% L%)` space syntax does NOT parse); `-webkit-` prefixes on transform/transition/animation and DUPLICATE every @keyframes as @-webkit-keyframes; px and % sizing (compute pixel sizes in JS from window.innerWidth/innerHeight, do not trust vw/vh/vmin).
DO NOT USE: CSS custom properties (var(--x)), calc(), env(), clamp(), CSS min()/max(); CSS `transform` on SVG child elements (animate an SVG shape by writing geometry attributes -- ry/cx/d -- with setAttribute, or the SVG `transform` ATTRIBUTE; never CSS transform on <g>/<path>).
`app/static/mini.html` in this repo is a known-good page under exactly these constraints.
No scroll on 320x480 AND rotated 480x320; keep a band clear top and bottom for the status bar and any text of your own, and make bottom text wrap or shrink so it is never clipped on the narrow portrait width.
