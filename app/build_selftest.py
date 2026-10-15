# -*- coding: utf-8 -*-
"""Run a page antons_build produced through a real browser and actually use it.

Same Chrome-spawn machinery as `browser_render` -- shares its `_LOCK` so a
build self-test and a `web_read` render never fight over one browser.
"""
import json
import subprocess
import time
from pathlib import Path

from app import browser_render as _br

try:
    from app.logging_setup import get_logger  # type: ignore
    LOGGER = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    LOGGER = logging.getLogger(__name__)

_SCRIPT = Path(__file__).resolve().parent / "_build_selftest.mjs"


def available(cfg=None):
    return bool(_br._node_bin() and _br._chrome_bin(cfg) and _SCRIPT.exists())


def run_selftest(url, cfg=None, wait_ms=3400, timeout=155, shots_dir=None):
    """Load `url`, exercise it, return a verdict dict. Never raises.

    {
      ok:        bool   -- ran and the page is not fatally broken
      passed:    int    -- checks that passed (smoke counts as one)
      total:     int
      smoke:     {pass, body_has_content, board_size, board_taps,
                  board_taps_self_reacted, responsive, runtime_errors:[...], ...}
      checks:    [{name, pass, detail}]   -- model-authored
      load_errors: [str]
      failures:  [str]  -- one line per failed check, ready to feed back to qwen
      error:     str
    }
    """
    node = _br._node_bin()
    chrome = _br._chrome_bin(cfg)
    if not (node and chrome and _SCRIPT.exists()):
        return {"ok": False, "passed": 0, "total": 0, "checks": [], "smoke": {},
                "load_errors": [], "failures": [],
                "error": "selftest unavailable (node=%s chrome=%s script=%s)"
                         % (bool(node), bool(chrome), _SCRIPT.exists())}

    args = [node, str(_SCRIPT), str(url), "--chrome", chrome,
            "--wait", str(int(max(1500, min(wait_ms, 9000))))]
    if shots_dir:
        args += ["--shots", str(shots_dir)]

    if not _br._LOCK.acquire(timeout=timeout + 5):
        return {"ok": False, "passed": 0, "total": 0, "checks": [], "smoke": {},
                "load_errors": [], "failures": [], "error": "browser busy"}
    started = time.time()
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (proc.stdout or "").strip()
        data = json.loads(out[out.index("{"):out.rindex("}") + 1]) if "{" in out else {}
    except subprocess.TimeoutExpired:
        _br._kill_orphans()
        return {"ok": False, "passed": 0, "total": 0, "checks": [], "smoke": {},
                "load_errors": [], "failures": ["self-test timed out"],
                "infra_failed": True, "error": "selftest timed out"}
    except Exception as exc:
        return {"ok": False, "passed": 0, "total": 0, "checks": [], "smoke": {},
                "load_errors": [], "failures": [], "infra_failed": True,
                "error": "selftest failed: %s" % str(exc)[:200]}
    finally:
        _br._LOCK.release()

    smoke = data.get("smoke") or {}
    checks = data.get("checks") or []
    load_errors = [e for e in (data.get("load_errors") or [])
                   if "favicon" not in str(e).lower()][:6]

    # how many of the model's OWN behaviour checks passed -- when >=3 do, they
    # vouch the page works, and a smoke probe that couldn't find a way to poke it
    # (a launcher page that is only navigation links, an unusual control scheme)
    # is the probe's blind spot, not a broken page. Same principle the
    # play-through already uses.
    _model_ok = sum(1 for c in checks if c.get("pass"))
    _vouched = _model_ok >= 3 or bool(smoke.get("board_driven"))

    failures = []
    if load_errors:
        failures.append("JS error on load: " + " | ".join(str(e)[:160] for e in load_errors[:3]))
    if smoke and smoke.get("pass") is False:
        if smoke.get("body_has_content") is False:
            failures.append("the page renders almost nothing (body is empty)")
        elif smoke.get("board_match_cleared") is False:
            failures.append(
                "a matched pair does NOT disappear: the game accepts the move "
                "(counter %s->%s) but the tiles stay on screen -- usually a CSS "
                "animation-fill overriding the removal transition, or the tile "
                "never leaves the DOM" % (smoke.get("board_left_before"),
                                          smoke.get("board_left_after")))
        elif smoke.get("layout_sane") is False:
            failures.append(
                "the tiles are not laid out correctly: %s. A new tile must "
                "appear IN its grid cell, not at the board corner; tiles must "
                "not overlap or render at 0x0 (a spawn animation that animates "
                "`transform` fights the `transform: translate()` that positions "
                "the tile, or metrics() computed a <=0 cell size)."
                % (smoke.get("layout_detail") or "tiles misplaced"))
        elif smoke.get("responsive") is False and not _vouched:
            bt, br_ = smoke.get("board_taps") or 0, smoke.get("board_taps_self_reacted") or 0
            if smoke.get("board_size", 0) >= 4 and not smoke.get("board_driven"):
                failures.append(
                    "the board does not respond to ANY input -- not a tap on a "
                    "cell (%d/%d reacted), not the arrow keys / WASD, not a swipe "
                    "across the board. A playable game must react to at least one "
                    "of these." % (br_, bt))
            elif bt:
                failures.append("tapping a playable board cell does nothing: only "
                                "%d of %d taps made the tapped cell react (a "
                                "selection glow, a class, a move). Expect nearly "
                                "all of them to." % (br_, bt))
            else:
                failures.append("clicking the controls does nothing visible")
        for e in (smoke.get("runtime_errors") or [])[:3]:
            failures.append("JS threw while using the page: " + str(e)[:160])
    if data.get("declared_error") and not checks:
        failures.append(
            "the <script id=\"selftest\"> block is broken (%s) so NONE of your "
            "own checks ran -- almost always a missing closing ] on the JSON "
            "array or a trailing comma. Fix the block: it must be one valid "
            "JSON array of {\"name\",\"js\"} objects." % data.get("declared_error"))
    for c in checks:
        if not c.get("pass"):
            failures.append("check FAILED: %s%s" % (
                c.get("name") or "?",
                (" -- " + str(c.get("detail"))[:160]) if c.get("detail") else ""))

    _blocked_seen = set()
    for where, taps in ([("after tapping around", smoke.get("taps"))]
                        + [("after «%s»" % (c.get("name") or "?"), c.get("taps"))
                           for c in checks]):
        for b in ((taps or {}).get("blocked") or []):
            if (b.get("what"), b.get("over")) in _blocked_seen:
                continue
            _blocked_seen.add((b.get("what"), b.get("over")))
            failures.append(
                "taps land on an invisible %s instead of the %s under it (%s of "
                "%s points across the screen, %s)%s. Something hidden must stop "
                "catching taps: pointer-events: none on it AND on its children "
                "(a child's own pointer-events: auto overrides its parent's "
                "none), or display: none." % (
                    b.get("what"), b.get("over"), b.get("points"),
                    (taps or {}).get("sampled"), where,
                    ("; %s hides it with opacity 0, but it still takes taps"
                     % b["via"]) if b.get("via") else ""))

    # the play-through: it reloads a fresh board and makes real moves. It is a
    # HEURISTIC -- when the smoke test already drove the board (arrow keys/swipe
    # moved it) or the model's own behaviour checks pass, a low real_moves here
    # is the tracker failing, not the game. Only trust "could not play" when
    # nothing else vouched for the game.
    play = data.get("play") or {}
    _other_vouch = _vouched
    if play.get("error"):
        failures.append("play-through crashed: " + str(play["error"])[:160])
    elif play and "no board" not in str(play.get("ended") or ""):
        for e in (play.get("errors_during_play") or [])[:3]:
            failures.append("JS threw while playing: " + str(e)[:160])
        _real = play.get("real_moves")
        _real = play.get("moves", 0) if _real is None else _real
        _reached_end = str(play.get("ended") or "").startswith("END:")
        if _real < 3 and not _other_vouch and not _reached_end:
            failures.append(
                "could not play the game: %d input(s) changed the board before it "
                "stalled (%s). Tried tapping cells, arrow keys, and swipes -- none "
                "moved the game forward." % (_real, play.get("ended") or "?"))
        elif play.get("advanced") is False and not _other_vouch:
            failures.append(
                "the game does not actually progress: played %d moves and the "
                "score never rose, no tiles merged, nothing cleared, no end "
                "state (peak score %s, biggest tile %s). Moves register but the "
                "core rule is not working." % (
                    _real, play.get("peak_score"), play.get("peak_max")))
        _dm, _dc = play.get("dir_moved"), play.get("dir_correct")
        if _dc is not None and _dm and _dm >= 2 and _dc < _dm:
            failures.append(
                "the arrow keys / swipes move the board the WRONG way: %d of %d "
                "directions that moved tiles went the wrong direction "
                "(deltas %s -- negative x = left, negative y = up; 'perp' = "
                "moved on the wrong axis). Left must move left, up must move up."
                % (_dm - _dc, _dm, play.get("dir_detail")))

    passed = int(data.get("passed") or 0)
    total = int(data.get("total") or 0)

    _cdp_dead = ("not attached", "target closed", "session with given id",
                 "no page target", "websocket", "evaluate timed out",
                 "cannot find context", "hard timeout", "no page target")
    _infra_soft = ("runner:", "smoke threw", "play threw", "play-through crashed")
    _cdp_hit = any(any(m in str(f).lower() for m in _cdp_dead) for f in failures)
    _soft_hits = sum(1 for f in failures
                     if any(m in str(f).lower() for m in (_cdp_dead + _infra_soft)))
    infra_failed = bool(data.get("infra_failed")) or (
        bool(total) and passed == 0 and _cdp_hit
        and _soft_hits >= max(2, total - 1))

    ok = (bool(data.get("ok"))
          and not (data.get("error"))
          and not infra_failed
          and not failures
          # all scored items pass, OR the only unscored gap is the play-through
          # heuristic and something stronger (smoke drive / model checks)
          # already vouched for the game
          and (passed == total or (_other_vouch and (total - passed) <= 1)))
    LOGGER.info("build selftest %s: passed=%d/%d ok=%s %.1fs play=%s real via=%s adv=%s dir=%s/%s failures=%d",
                url, passed, total, ok, time.time() - started,
                play.get("real_moves", play.get("moves")),
                play.get("method") or smoke.get("board_drive_method") or "?",
                play.get("advanced"), play.get("dir_correct"), play.get("dir_moved"),
                len(failures))
    return {
        "ok": ok,
        "passed": passed,
        "total": total,
        "infra_failed": bool(infra_failed),
        "smoke": smoke,
        "checks": checks,
        "play": play,
        "screenshots": [s.get("path") for s in (data.get("screenshots") or []) if s.get("path")],
        "load_errors": load_errors,
        "failures": failures[:8],
        "error": str(data.get("error") or ""),
    }


def summary_line(result):
    """One short line for the chat report / ledger, with the page's Playwright
    run (app/page_tests.py) shown apart from the self-test's own count."""
    line = _summary_core(result)
    pw = (result or {}).get("playwright") or {}
    if pw.get("total"):
        line += " · Playwright %d/%d" % (pw.get("passed", 0), pw["total"])
    elif pw.get("error"):
        line += " · Playwright: nevarēja palaist"
    return line


def _summary_core(result):
    if not result or result.get("error") and not result.get("total"):
        return "testi: nevarēja palaist"
    p, t = result.get("passed", 0), result.get("total", 0)
    if not t:
        return "testi: nav"
    if p == t and not result.get("failures"):
        return "testi: %d/%d OK" % (p, t)
    names = "; ".join(f[:80] for f in (result.get("failures") or [])[:3])
    if not names:
        gaps = []
        if (result.get("smoke") or {}).get("pass") is False:
            gaps.append("pieskārienu tests")
        play = result.get("play") or {}
        _real = play.get("real_moves", play.get("moves")) or 0
        if (play and "error" not in play and "no board" not in str(play.get("ended") or "")
                and (_real < 3 or play.get("errors_during_play"))):
            gaps.append("automātiskā izspēle")
        names = ", ".join(gaps)
    return "testi: %d/%d (krīt: %s)" % (p, t, names or "?")
