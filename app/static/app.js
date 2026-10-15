(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return document.querySelectorAll(s); };

  var paused = false;
  var data = null;
  var online = false;
  var _fanPending = {};
  var HIST = 60;
  var hist = { cpu: [], gpu: [], vram: [], ram: [], cput: [], gput: [], netrx: [], nettx: [] };
  // The shell decides which tab opens first — console starts on Overview,
  // the personal app (/me) starts on Assistant.
  var _firstNav = document.querySelector(".nb.active");
  var activeTab = (_firstNav && _firstNav.dataset && _firstNav.dataset.t) || "overview";

  function push(k, v) {
    var a = hist[k];
    a.push(isFiniteNumber(v) ? v : 0);
    if (a.length > HIST) a.shift();
  }

  // ---------- formatting helpers (must stay byte-clean: no backtick-wrapped quotes) ----------
  function isFiniteNumber(v) { return typeof v === "number" && isFinite(v) && v !== -1; }
  function num(v, d) { return isFiniteNumber(v) ? (+v).toFixed(d == null ? 1 : d) : "-"; }
  function intgr(v) { return isFiniteNumber(v) ? Math.round(v).toString() : "-"; }
  function pctTxt(v) { return isFiniteNumber(v) ? Math.min(Math.max(v, 0), 100).toFixed(1) + "%" : "-"; }
  function gb(v) { return isFiniteNumber(v) ? (+v).toFixed(1) + " GB" : "-"; }
  function mbToGb(v) { return isFiniteNumber(v) ? (v / 1024).toFixed(1) + " GB" : "-"; }
  function tempTxt(v) { return isFiniteNumber(v) ? Math.round(v) + "°C" : "-"; }
  function watt(v) { return isFiniteNumber(v) ? Math.round(v) + " W" : "-"; }
  function mhz(v) { return isFiniteNumber(v) ? Math.round(v) + " MHz" : "-"; }
  function rate(v) {
    if (!isFiniteNumber(v)) return "-";
    if (v < 1024) return v.toFixed(0) + " KB/s";
    return (v / 1024).toFixed(2) + " MB/s";
  }
  function safe(v) { return (v === null || v === undefined || v === "") ? "-" : String(v); }
  function clampPct(v) { return isFiniteNumber(v) ? Math.min(Math.max(v, 0), 100) : 0; }
  function dur(s) {
    if (!isFiniteNumber(s)) return "-";
    s = Math.floor(s);
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + "d " + h + "h";
    if (h > 0) return h + "h " + m + "m";
    return m + "m " + (s % 60) + "s";
  }
  function lvl(v, a, r) { return v >= r ? "red" : v >= a ? "amber" : "green"; }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function parseJsonResponse(r) {
    return r.text().then(function (text) {
      var body = {};
      if (text) {
        try { body = JSON.parse(text); }
        catch (e) { body = { ok: false, error: text.slice(0, 800) }; }
      }
      if (!body || typeof body !== "object") body = { ok: false, output: String(body || "") };
      body._status = r.status;
      body._httpOk = r.ok;
      return body;
    });
  }
  function responseError(j, fallback) {
    if (j && (j.error || j.output)) return String(j.error || j.output).slice(0, 800);
    if (j && j._status) return fallback + " (HTTP " + j._status + ")";
    return fallback;
  }
  function hexA(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }

  // Muted status palette (matches styles.css vars) — no neon gradients.
  var COL = {
    blue: ["#539bf5", "#6cabf7"], green: ["#4fb469", "#66c07e"],
    amber: ["#d4a72c", "#ddb54f"], red: ["#e5534b", "#ea6f68"],
    cyan: ["#59b8d4", "#74c5dd"]
  };
  function gcol(v, a, r) {
    var L = lvl(v, a, r);
    return L === "red" ? COL.red : L === "amber" ? COL.amber : COL.green;
  }

  // ---------- SVG radial gauge ----------
  var GID = 0;
  function gauge(value, label, sub, color) {
    var p = clampPct(value), R = 52, C = 2 * Math.PI * R, off = C * (1 - p / 100);
    var id = "grad" + (GID++);
    return '' +
      '<div class="gcard gauge">' +
        '<svg viewBox="0 0 120 120">' +
          '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="1" y2="1">' +
            '<stop offset="0" stop-color="' + color[0] + '"/>' +
            '<stop offset="1" stop-color="' + color[1] + '"/>' +
          '</linearGradient></defs>' +
          '<circle class="g-track" cx="60" cy="60" r="' + R + '" fill="none" stroke-width="11"/>' +
          '<circle class="g-fill" cx="60" cy="60" r="' + R + '" fill="none" stroke="url(#' + id + ')" ' +
            'stroke-width="11" stroke-linecap="round" stroke-dasharray="' + C + '" ' +
            'stroke-dashoffset="' + off + '" transform="rotate(-90 60 60)" style="color:' + color[1] + '"/>' +
          '<text class="g-val" x="60" y="58" text-anchor="middle">' + Math.round(p) + '</text>' +
          '<text class="g-unit" x="60" y="74" text-anchor="middle">%</text>' +
        '</svg>' +
        '<div class="gauge-label">' + esc(label) + '</div>' +
        '<div class="gauge-sub">' + sub + '</div>' +
      '</div>';
  }

  // ---------- canvas sparkline ----------
  function spark(canvas, arr, c1, c2) {
    if (!canvas) return;
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 240, h = canvas.clientHeight || 34;
    canvas.width = w * dpr; canvas.height = h * dpr;
    var x = canvas.getContext("2d");
    x.scale(dpr, dpr); x.clearRect(0, 0, w, h);
    if (!arr || arr.length < 2) return;
    var mx = Math.max.apply(null, arr.concat([1]));
    var mn = Math.min.apply(null, arr.concat([0]));
    var rng = (mx - mn) || 1;
    var px = function (i) { return i / (arr.length - 1) * w; };
    var py = function (v) { return h - 3 - ((v - mn) / rng) * (h - 6); };
    x.beginPath(); x.moveTo(0, py(arr[0]));
    for (var i = 1; i < arr.length; i++) x.lineTo(px(i), py(arr[i]));
    var g = x.createLinearGradient(0, 0, w, 0);
    g.addColorStop(0, c1); g.addColorStop(1, c2);
    x.strokeStyle = g; x.lineWidth = 2; x.lineJoin = "round"; x.stroke();
    x.lineTo(w, h); x.lineTo(0, h); x.closePath();
    var fg = x.createLinearGradient(0, 0, 0, h);
    fg.addColorStop(0, hexA(c2, 0.14)); fg.addColorStop(1, hexA(c2, 0));
    x.fillStyle = fg; x.fill();
  }

  function miniCard(label, val, valClass, sub, canvasId) {
    return '<div class="gcard mini">' +
      '<div class="m-top"><span class="m-label">' + label + '</span></div>' +
      '<div class="m-val ' + valClass + '">' + val + '</div>' +
      '<div class="m-sub">' + sub + '</div>' +
      '<canvas class="spark" id="' + canvasId + '"></canvas>' +
    '</div>';
  }
  function rowsHTML(pairs) {
    var h = '<div class="rows">';
    for (var i = 0; i < pairs.length; i++) {
      h += '<div class="row"><span class="k">' + pairs[i][0] +
           '</span><span class="v">' + pairs[i][1] + '</span></div>';
    }
    return h + '</div>';
  }
  function bar(p, a, r) {
    return '<div class="bar-wrap"><div class="bar ' + lvl(p, a, r) +
      '" style="width:' + clampPct(p) + '%"></div></div>';
  }
  // Fixed-colour progress bar (no warn/crit thresholds) — e.g. download %.
  // ---------- renderers ----------
  function renderOverview(d) {
    if (!$("#hero")) return;
    var cpu = d.cpu || {}, gpu = d.gpu || {}, mem = d.memory || {};
    var cpuP = d.cpu_percent, gpuP = gpu.gpu_utilization_percent;
    var vramP = gpu.memory_utilization_percent, ramP = mem.percent;
    var cpuT = cpu.package_temperature_c != null ? cpu.package_temperature_c : cpu.max_c;

    var hero = $("#hero");
    hero.innerHTML =
      gauge(cpuP, "CPU", tempTxt(cpuT) + " &middot; " + mhz(cpu.frequency_mhz), gcol(cpuP, 60, 85)) +
      gauge(gpuP, "GPU", tempTxt(gpu.temperature_c) + " &middot; " + watt(gpu.power_draw_w), gcol(gpuP, 60, 90)) +
      gauge(vramP, "VRAM", mbToGb(gpu.memory_used_mb) + " / " + mbToGb(gpu.memory_total_mb), gcol(vramP, 70, 90)) +
      gauge(ramP, "RAM", gb(mem.used_gb) + " / " + gb(mem.total_gb), gcol(ramP, 70, 90));

    var net0 = (d.network && d.network[0]) || {};
    var ov = $("#ov-extra");
    ov.innerHTML =
      miniCard("CPU Load", pctTxt(cpuP), "val-" + lvl(cpuP, 60, 85), "avg all cores", "sp_cpu") +
      miniCard("GPU Load", pctTxt(gpuP), "val-" + lvl(gpuP, 60, 90), safe(gpu.name), "sp_gpu") +
      miniCard("CPU Temp", tempTxt(cpuT), "val-" + lvl(cpuT, 70, 85), "pkg / max " + tempTxt(cpu.max_c), "sp_cput") +
      miniCard("GPU Temp", tempTxt(gpu.temperature_c), "val-" + lvl(gpu.temperature_c, 70, 83), "fan " + pctTxt(gpu.fan_speed_percent), "sp_gput") +
      miniCard("Net Down", rate(net0.rx_rate_kbps), "val-cyan", safe(net0.name), "sp_rx") +
      miniCard("Net Up", rate(net0.tx_rate_kbps), "val-cyan", "uptime " + dur(d.system_uptime_seconds), "sp_tx");

    spark($("#sp_cpu"), hist.cpu, COL.blue[0], COL.blue[1]);
    spark($("#sp_gpu"), hist.gpu, COL.blue[0], COL.blue[1]);
    spark($("#sp_cput"), hist.cput, COL.amber[0], COL.amber[1]);
    spark($("#sp_gput"), hist.gput, COL.amber[0], COL.amber[1]);
    spark($("#sp_rx"), hist.netrx, COL.cyan[0], COL.cyan[1]);
    spark($("#sp_tx"), hist.nettx, COL.green[0], COL.green[1]);
  }

  var asstState = { lat: null, digest: null, queue: null };

  function renderAssistant() {
    var el = $("#ov-assistant");
    if (!el) return;
    var lat = asstState.lat, dig = asstState.digest, q = asstState.queue;
    if (!lat && !dig) { el.innerHTML = ""; return; }
    function secs(v) { return v == null ? "&mdash;" : Math.round(v) + "s"; }
    var answered = lat ? (lat.answered_turns || 0) : 0;
    var silent = lat ? (lat.silent_turns || 0) : 0;
    var reloads = lat ? (lat.reloads || 0) : 0;
    var digTxt = "off", digSub = "disabled";
    if (dig && dig.config && dig.config.enabled) {
      digTxt = String(dig.config.hour != null ? dig.config.hour : "?") + ":00";
      digSub = ((dig.config.targets || []).length) + " chats";
      if (dig.last_sent) digSub += " &middot; " + esc(String(dig.last_sent).slice(5, 10));
    }
    var qTxt = q && q.length ? q.length + " active" : "idle";
    el.innerHTML =
      '<div class="ogrid">' +
        miniCard("Answer time", secs(lat && lat.median_answer_seconds),
                 "val-" + lvl(lat && lat.median_answer_seconds, 45, 90),
                 "p90 " + secs(lat && lat.p90_answer_seconds), "sp_ans") +
        miniCard("Replies", String(answered),
                 "val-cyan", silent + " silent", "sp_rep") +
        miniCard("Model reloads", String(reloads),
                 "val-" + lvl(reloads, 5, 20), "cost " + secs(lat && lat.reload_seconds), "sp_rel") +
        miniCard("Digest", digTxt, "val-cyan", digSub, "sp_dig") +
      '</div>';
  }

  function fetchAssistant() {
    fetch("/api/messaging/latency", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { asstState.lat = j; renderAssistant(); })
      .catch(function () {});
    fetch("/api/messaging/digest", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { asstState.digest = j; renderAssistant(); })
      .catch(function () {});
  }

  // Compact AI/services status strip on the overview.
  function renderEco() {
    var el = $("#ov-eco");
    if (!el) return;
    var svc = ecoState.services, ml = ecoState.modelLoaded;
    if (!svc) { el.innerHTML = ""; return; }
    var chips = "";
    for (var i = 0; i < svc.length; i++) {
      var s = svc[i];
      chips += '<span class="svc-chip ' + (s.up ? "on" : "off") + '">' +
        '<i class="dot"></i>' + esc(s.name) + '</span>';
    }
    var modelTxt = ml && ml.length ? esc(ml[0].name) : "idle (loads on demand)";
    el.innerHTML =
      '<div class="gcard eco">' +
        '<div class="eco-row">' +
          '<div class="eco-k">Assistant model</div>' +
          '<div class="eco-v">' + modelTxt + '</div>' +
        '</div>' +
        '<div class="svc-strip">' + chips + '</div>' +
      '</div>';
  }

  function renderGpu(d) {
    var g = d.gpu || {};
    var c = $("#gpu-card");
    if (!g.available) { c.innerHTML = '<div class="gcard"><div class="muted">No NVIDIA GPU detected.</div></div>'; return; }
    var vramP = g.memory_utilization_percent, utilP = g.gpu_utilization_percent;
    var powP = (g.power_draw_w && g.power_limit_w) ? g.power_draw_w / g.power_limit_w * 100 : 0;
    var procs = g.processes || [];
    var ph = '';
    if (procs.length) {
      ph = '<div class="gcard"><div class="card-h">VRAM Processes</div><div class="plist">';
      for (var i = 0; i < procs.length; i++) {
        ph += '<div class="prow"><span class="pn">' + esc(procs[i].name) +
          '</span><span class="pp">#' + esc(procs[i].pid) + '</span><span class="pv">' +
          mbToGb(procs[i].memory_mb) + '</span></div>';
      }
      ph += '</div></div>';
    }
    c.innerHTML =
      '<div class="stack">' +
        '<div class="gcard"><div class="card-h">' + esc(g.name) + '</div>' +
          rowsHTML([
            ["GPU Utilization", pctTxt(utilP)],
          ]) + bar(utilP, 60, 90) +
          rowsHTML([["VRAM", mbToGb(g.memory_used_mb) + " / " + mbToGb(g.memory_total_mb) + " (" + pctTxt(vramP) + ")"]]) +
          bar(vramP, 70, 90) +
          rowsHTML([["Power", watt(g.power_draw_w) + " / " + watt(g.power_limit_w)]]) +
          bar(powP, 70, 92) +
        '</div>' +
        '<div class="gcard"><div class="card-h">Sensors &amp; Clocks</div>' +
          rowsHTML([
            ["Core Temp", tempTxt(g.temperature_c)],
            ["Fan Speed", pctTxt(g.fan_speed_percent)],
            ["Graphics Clock", mhz(g.clock_graphics_mhz)],
            ["Memory Clock", mhz(g.clock_memory_mhz)],
            ["SM Clock", mhz(g.clock_sm_mhz)],
            ["Performance State", safe(g.pstate)],
            ["VRAM Free", mbToGb(g.memory_free_mb)],
            ["Driver", safe(g.driver_version)]
          ]) +
        '</div>' +
        renderGpuFan(d) +
        ph +
      '</div>';
  }

  function thermalBanner(f) {
    if (!f || !f.thermal_emergency) return '';
    var peak = (f.peak_temp_c != null) ? Math.round(f.peak_temp_c) + "°C" : "unknown";
    var crit = f.critical_temp_c || 95;
    var rec = f.recovery_temp_c || 85;
    return '<div class="emergency-banner">THERMAL EMERGENCY: CPU peak ' + peak +
           ' &ge; ' + crit + '°C - all fans locked at 100% until below ' + rec + '°C</div>';
  }

  function renderFanCards(f, device, hiddenChannels) {
    if (!f || !f.available) {
      var err = (f && f.error) ? String(f.error) : "fan control offline";
      var fix;
      if (f && f.elevated === false) {
        // Served by an unelevated dev process: retrying can't help, only the
        // real service can reach the chip. Explain the one-step fix.
        fix = '<div class="fan-fix">This page is being served by a dev process, not the ' +
          'SysMonitor service, so the fan chip is unreachable. Run ' +
          '<code>_service\\revive.cmd</code> on the rig (one UAC click) to hand back to the service.</div>';
      } else if (/wedged|restart required/i.test(err)) {
        fix = '<div class="fan-fix"><button class="btn-sm fan-restart-btn" type="button">Restart service</button></div>';
      } else {
        fix = '<div class="fan-fix"><button class="btn-sm fan-rescan-btn" type="button">Retry fan detection</button></div>';
      }
      return thermalBanner(f) + '<div class="gcard"><div class="card-h">' +
             (device === 'gpu' ? 'GPU Fan' : 'Fan Control') +
             '</div><div class="muted">Unavailable: ' + esc(err) + '</div>' + fix +
             '<div class="fan-msg"></div></div>';
    }
    var channels = (f.channels || []).filter(function (ch) {
      if (hiddenChannels && hiddenChannels[ch.index]) return false;
      if (device === "gpu") return true;
      return (ch.rpm != null && ch.rpm > 0) ||
             (ch.pwm_percent != null && ch.pwm_percent > 0);
    });
    if (!channels.length) {
      return thermalBanner(f) + '<div class="gcard"><div class="card-h">' +
             (device === 'gpu' ? 'GPU Fan' : 'Fan Control') +
             '</div><div class="muted">No active fan channels.</div></div>';
    }
    var modes = ["auto", "silent", "quiet", "balanced", "performance"];
    var labels = { auto: "Auto", silent: "Silent", quiet: "Quiet", balanced: "Balanced", performance: "Max" };
    var emergency = !!f.thermal_emergency;
    var h = thermalBanner(f);
    for (var c = 0; c < channels.length; c++) {
      var ch = channels[c];
      var idx = ch.index;
      var pending = _fanPending[device + ":" + idx];
      var current = (pending && pending.until > Date.now()) ? pending.mode : ch.mode;
      var rpmTxt = (ch.rpm != null) ? Math.round(ch.rpm) + " RPM" : "-- RPM";
      var pwmTxt = (ch.pwm_percent != null) ? "PWM " + Math.round(ch.pwm_percent) + "%" : "default";
      h += '<div class="gcard fan-card' + (emergency ? ' locked' : '') +
           '" data-channel="' + idx + '" data-device="' + device + '">' +
           '<div class="card-h">' + esc(ch.name) + ' &middot; ch ' + idx + '</div>' +
           '<div class="fan-meter">' +
             '<div class="fan-rpm">' + rpmTxt + '</div>' +
             '<div class="fan-pwm">' + pwmTxt + '</div>' +
           '</div>' +
           '<div class="seg">';
      for (var i = 0; i < modes.length; i++) {
        var m = modes[i];
        h += '<button class="sb' + (m === current ? ' on' : '') + '" data-mode="' + m +
             '" data-channel="' + idx + '" data-device="' + device +
             '"' + (emergency ? ' disabled' : '') + '>' + labels[m] + '</button>';
      }
      h += '</div><div class="fan-msg"></div></div>';
    }
    return h;
  }

  function renderFan(d) {
    // Dead PWM-only header on this rig (ch 5 sits at ~50% PWM, no fan).
    return renderFanCards((d.cpu && d.cpu.fan) || {}, 'cpu', { 5: true });
  }

  function renderGpuFan(d) {
    return renderFanCards((d.gpu && d.gpu.fan) || {}, 'gpu', null);
  }

  function renderCpu(d) {
    if (!$("#cpu-card")) return;
    var cpu = d.cpu || {};
    var temps = cpu.core_temperatures_c || [];
    var loads = cpu.core_loads_percent || [];
    var per = cpu.per_core_percent || [];
    var coreCount = cpu.core_count || temps.length || Math.floor(per.length / 2) || per.length;
    var coreH = '';
    if (coreCount) {
      coreH = '<div class="gcard"><div class="card-h">Per-Core</div><div class="cores">';
      for (var i = 0; i < coreCount; i++) {
        var t = temps[i];
        var ld = null;
        if (loads.length >= coreCount * 2) {
          var a = loads[2 * i], b = loads[2 * i + 1];
          if (a != null && b != null) ld = (a + b) / 2;
          else if (a != null) ld = a;
          else if (b != null) ld = b;
        } else if (loads[i] != null) {
          ld = loads[i];
        } else if (per.length >= coreCount * 2) {
          var pa = per[2 * i], pb = per[2 * i + 1];
          if (pa != null && pb != null) ld = (pa + pb) / 2;
        } else if (per[i] != null) {
          ld = per[i];
        }
        coreH += '<div class="core"><div class="ci">C' + i + '</div><div class="ct val-' +
          lvl(t, 70, 85) + '">' + tempTxt(t) + '</div><div class="cl">' +
          (ld != null ? pctTxt(ld) : "-") + '</div></div>';
      }
      coreH += '</div></div>';
    }
    var t = cpu.times_percent || {};
    $("#cpu-card").innerHTML =
      '<div class="stack">' +
        '<div class="gcard"><div class="card-h">' + esc(cpu.model || "CPU") + '</div>' +
          rowsHTML([["Total Load", pctTxt(d.cpu_percent)]]) + bar(d.cpu_percent, 60, 85) +
          rowsHTML([
            ["Package Temp", tempTxt(cpu.package_temperature_c)],
            ["Average Temp", tempTxt(cpu.average_temperature_c)],
            ["Max Temp", tempTxt(cpu.max_c)],
            ["TjMax", tempTxt(cpu.tjmax_c)],
            ["Cores / Threads", safe(cpu.core_count) + " / " + safe(cpu.thread_count)],
            ["Frequency", mhz(cpu.frequency_mhz) + (cpu.frequency_max_mhz ? " / " + mhz(cpu.frequency_max_mhz) : "")],
            ["Multiplier", safe(cpu.multiplier) + (cpu.bus_speed_mhz ? " @ " + num(cpu.bus_speed_mhz, 0) + " MHz" : "")],
            ["Core Voltage", cpu.vid_v != null ? num(cpu.vid_v, 3) + " V" : "-"],
            ["Load Avg", cpu.load_average ? cpu.load_average.join("  ") : "-"]
          ]) +
        '</div>' +
        renderFan(d) +
        '<div class="gcard"><div class="card-h">CPU Time</div>' +
          rowsHTML([
            ["User", pctTxt(t.user)],
            ["System", pctTxt(t.system)],
            ["Idle", pctTxt(t.idle)],
            ["Ctx Switches", cpu.stats ? safe(cpu.stats.ctx_switches) : "-"],
            ["Interrupts", cpu.stats ? safe(cpu.stats.interrupts) : "-"]
          ]) +
        '</div>' +
        coreH +
      '</div>';
  }

  function renderMem(d) {
    if (!$("#mem-card")) return;
    var m = d.memory || {}, sw = m.swap || {};
    $("#mem-card").innerHTML =
      '<div class="stack">' +
        '<div class="gcard"><div class="card-h">RAM</div>' +
          '<div class="m-val val-' + lvl(m.percent, 70, 90) + '" style="font-size:30px">' + pctTxt(m.percent) + '</div>' +
          '<div class="m-sub">' + gb(m.used_gb) + " used of " + gb(m.total_gb) + '</div>' +
          bar(m.percent, 70, 90) +
          rowsHTML([
            ["Total", gb(m.total_gb)],
            ["Used", gb(m.used_gb)],
            ["Available", gb(m.available_gb)]
          ]) +
        '</div>' +
        '<div class="gcard"><div class="card-h">Swap / Page File</div>' +
          (sw && sw.total_gb != null ?
            bar(sw.percent, 50, 80) + rowsHTML([
              ["Total", gb(sw.total_gb)],
              ["Used", gb(sw.used_gb)],
              ["Usage", pctTxt(sw.percent)]
            ]) : '<div class="muted">No swap data.</div>') +
        '</div>' +
      '</div>';
  }

  function renderDisks(d) {
    if (!$("#disks-card")) return;
    var disks = d.disks || [], io = d.disk_io || {};
    var h = '<div class="stack">';
    var sorted = disks.slice().sort(function (a, b) { return (b.percent || 0) - (a.percent || 0); });
    for (var i = 0; i < sorted.length; i++) {
      var dk = sorted[i];
      h += '<div class="gcard"><div class="card-h">Drive ' + esc(dk.letter) +
        (dk.fstype ? " &middot; " + esc(dk.fstype) : "") + '</div>' +
        '<div class="m-val val-' + lvl(dk.percent, 80, 92) + '" style="font-size:24px">' +
        pctTxt(dk.percent) + '</div>' + bar(dk.percent, 80, 92) +
        rowsHTML([
          ["Used", gb(dk.used_gb)],
          ["Free", gb(dk.free_gb)],
          ["Total", gb(dk.total_gb)]
        ]) + '</div>';
    }
    if (!sorted.length) h += '<div class="gcard"><div class="muted">No disks found.</div></div>';
    if (io && io.read_mb != null) {
      h += '<div class="gcard"><div class="card-h">Disk I/O</div>' +
        rowsHTML([
          ["Read Rate", num(io.read_rate_mbps, 2) + " MB/s"],
          ["Write Rate", num(io.write_rate_mbps, 2) + " MB/s"],
          ["Total Read", gb(io.read_mb / 1024)],
          ["Total Written", gb(io.write_mb / 1024)],
          ["Read Ops", safe(io.read_count)],
          ["Write Ops", safe(io.write_count)]
        ]) + '</div>';
    }
    $("#disks-card").innerHTML = h + '</div>';
  }

  function renderNet(d) {
    if (!$("#net-card")) return;
    var nets = d.network || [];
    var h = '<div class="stack">';
    var sorted = nets.slice().sort(function (a, b) {
      return ((b.rx_rate_kbps || 0) + (b.tx_rate_kbps || 0)) - ((a.rx_rate_kbps || 0) + (a.tx_rate_kbps || 0));
    });
    for (var i = 0; i < sorted.length; i++) {
      var nx = sorted[i];
      h += '<div class="gcard"><div class="card-h">' + esc(nx.name) + '</div>' +
        '<div class="grid2">' +
          '<div><div class="m-label">DOWN</div><div class="m-val val-cyan">' + rate(nx.rx_rate_kbps) + '</div></div>' +
          '<div><div class="m-label">UP</div><div class="m-val val-green">' + rate(nx.tx_rate_kbps) + '</div></div>' +
        '</div>' +
        rowsHTML([
          ["Total Received", gb((nx.rx_kb || 0) / 1048576)],
          ["Total Sent", gb((nx.tx_kb || 0) / 1048576)],
          ["Packets In / Out", safe(nx.packets_recv) + " / " + safe(nx.packets_sent)],
          ["Errors / Drops", safe(nx.errors) + " / " + safe(nx.drops)]
        ]) + '</div>';
    }
    if (!sorted.length) h += '<div class="gcard"><div class="muted">No network interfaces.</div></div>';
    $("#net-card").innerHTML = h + '</div>';
  }

  function renderProc(d) {
    if (!$("#proc-card")) return;
    var p = d.processes || {};
    var byCpu = p.by_cpu || [], byMem = p.by_memory || [];
    var gproc = (d.gpu && d.gpu.processes) || [];
    function list(arr, key, fmt) {
      if (!arr.length) return '<div class="muted">No data.</div>';
      var s = '<div class="plist">';
      for (var i = 0; i < arr.length; i++) {
        s += '<div class="prow"><span class="pn">' + esc(arr[i].name) +
          '</span><span class="pp">#' + esc(arr[i].pid) + '</span><span class="pv">' +
          fmt(arr[i][key]) + '</span></div>';
      }
      return s + '</div>';
    }
    $("#proc-card").innerHTML =
      '<div class="stack">' +
        '<div class="gcard"><div class="card-h">Processes (' + safe(p.total) + ' running)</div>' +
          '<div class="m-label" style="margin-bottom:6px">TOP BY CPU</div>' +
          list(byCpu, "cpu", function (v) { return pctTxt(v); }) +
        '</div>' +
        '<div class="gcard"><div class="card-h">Top By Memory</div>' +
          list(byMem, "mem", function (v) { return pctTxt(v); }) +
        '</div>' +
        '<div class="gcard"><div class="card-h">GPU Compute Apps</div>' +
          (gproc.length ? list(gproc, "memory_mb", function (v) { return mbToGb(v); })
                        : '<div class="muted">Nothing using the GPU.</div>') +
        '</div>' +
      '</div>';
  }

  // Lives on the Overview now: silent when healthy, a compact strip when not.
  function renderWarn(d) {
    var el = $("#warn-card");
    if (!el) return;
    var w = (d.warnings || []).slice();
    var fan = (d.cpu && d.cpu.fan) || {};
    if (fan.available === false && fan.elevated === false) {
      w.push("Dev mode: served by an unelevated process — fan control is off. Run _service\\revive.cmd to hand back to the service.");
    }
    if (!w.length) { el.innerHTML = ""; return; }
    var h = '<div class="warn-strip">';
    for (var i = 0; i < w.length; i++) h += '<div class="warn-item">' + esc(w[i]) + '</div>';
    el.innerHTML = h + '</div>';
  }

  function renderRaw(d) {
    var r = $("#raw-data");
    if (r) r.textContent = JSON.stringify(d, null, 2);
  }

  var SERVICE_URL = {
    open_webui: "http://" + location.hostname + ":3000",
    comfyui: "http://" + location.hostname + ":8188",
    searxng: "http://" + location.hostname + ":8888",
    ollama: "http://" + location.hostname + ":11434",
    media: "",
    jellyfin: ""
  };
  // the media box is another machine; its addresses come from the server's environment
  fetch("/api/hosts").then(function (r) { return r.json(); }).then(function (h) {
    SERVICE_URL.media = h.media_console || "";
    SERVICE_URL.jellyfin = h.jellyfin || "";
  }).catch(function () {});
  var ecoState = { services: null, models: null, modelLoaded: null, loadedAt: 0 };
  var messagingState = { loaded: false, busy: false, status: null, outbox: [], lastSend: null, autoReply: null, inbox: [] };
  var hubBusy = {};
  var svcErr = {};

  function fetchHub() {
    fetch("/api/services", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        ecoState.services = j.services || [];
        renderEco();
        if (activeTab === "aihub") renderAihub();
        if (activeTab === "studio") renderStudioStatus();
      })
      .catch(function () {});
    fetch("/api/ollama/models", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        ecoState.models = j.models || [];
        ecoState.modelLoaded = j.loaded || [];
        populateChatModels();
        renderEco();
        if (activeTab === "aihub") renderAihub();
      })
      .catch(function () {});
    fetchMessagingStatus(false);
    fetchAssistant();
  }

  function skeleton(rows) {
    var h = '<div class="gcard"><div class="sk-line sk-h"></div>';
    for (var i = 0; i < (rows || 3); i++) h += '<div class="sk-line"></div>';
    return h + '</div>';
  }

  function renderAihub() {
    var el = $("#aihub-card");
    if (!el) return;
    // Nothing fetched yet → skeleton instead of an empty flash.
    if (ecoState.services === null && ecoState.models === null) {
      el.innerHTML = '<div class="stack">' + skeleton(4) + skeleton(5) + '</div>';
      return;
    }
    var svc = ecoState.services || [];
    var models = ecoState.models || [];
    var loaded = ecoState.modelLoaded || [];
    var loadedNames = {};
    for (var i = 0; i < loaded.length; i++) loadedNames[loaded[i].name] = loaded[i];

    var svcH = '<div class="gcard"><div class="card-h">Services</div><div class="svc-grid">';
    for (var s = 0; s < svc.length; s++) {
      var v = svc[s];
      var url = SERVICE_URL[v.id];
      var busyS = hubBusy["svc:" + v.id];
      var actions = "";
      if (v.control) {
        if (busyS) {
          actions += '<button class="svc-ctl busy" disabled>' + esc(busyS) + '...</button>';
        } else if (v.up) {
          actions += '<button class="svc-ctl stop" data-svc="' + v.id + '" data-act="stop">Stop</button>';
        } else {
          actions += '<button class="svc-ctl start" data-svc="' + v.id + '" data-act="start">Start</button>';
        }
      }
      if (url && v.up) {
        actions += '<a class="svc-open" href="' + url + '" target="_blank" rel="noopener">Open &#8599;</a>';
      }
      svcH += '<div class="svc-tile ' + (v.up ? "up" : "down") + '">' +
        '<div class="svc-top"><span class="svc-name">' + esc(v.name) + '</span>' +
          '<span class="svc-state ' + (v.up ? "on" : "off") + '">' + (v.up ? "online" : "offline") + '</span></div>' +
        '<div class="svc-desc">' + esc(v.desc) + '</div>' +
        '<div class="svc-port">' + esc(v.host) + ':' + v.port + '</div>' +
        (svcErr[v.id] ? '<div class="svc-err">' + esc(svcErr[v.id]) + '</div>' : '') +
        (actions ? '<div class="svc-actions">' + actions + '</div>' : '') +
      '</div>';
    }
    svcH += '</div></div>';

    var modH = '<div class="gcard"><div class="card-h">Ollama Models</div>';
    if (!models.length) {
      modH += '<div class="muted">No models found (or Ollama offline).</div>';
    } else {
      modH += '<div class="plist">';
      for (var m = 0; m < models.length; m++) {
        var mm = models[m];
        var isLoaded = !!loadedNames[mm.name];
        var busy = hubBusy[mm.name];
        var meta = [];
        if (mm.params) meta.push(mm.params);
        if (mm.quant) meta.push(mm.quant);
        meta.push(mm.size_gb + " GB");
        var btn;
        if (busy) {
          btn = '<button class="mdl-btn busy" disabled>' + esc(busy) + '...</button>';
        } else if (isLoaded) {
          btn = '<button class="mdl-btn unload" data-model="' + esc(mm.name) + '" data-act="unload">Unload</button>';
        } else {
          btn = '<button class="mdl-btn load" data-model="' + esc(mm.name) + '" data-act="load">Load</button>';
        }
        modH += '<div class="mdl-row' + (isLoaded ? " loaded" : "") + '">' +
          '<div class="mdl-main"><span class="mdl-name">' + esc(mm.name) + '</span>' +
          '<span class="mdl-meta">' + esc(meta.join(" · ")) + '</span></div>' +
          (isLoaded ? '<span class="mdl-live">in VRAM</span>' : '') + btn +
        '</div>';
      }
      modH += '</div>';
    }
    modH += '</div>';

    el.innerHTML = '<div class="stack">' + svcH + modH + '</div>';
  }

  function fetchMessagingStatus(renderNow) {
    fetch("/api/messaging/status", { cache: "no-store" }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "messaging status failed"));
      messagingState.status = j; messagingState.loaded = true;
      return fetch("/api/messaging/outbox", { cache: "no-store" });
    }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) messagingState.outbox = j.items || [];
      return fetch("/api/messaging/autoreply", { cache: "no-store" });
    }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) messagingState.autoReply = j;
      fetch("/api/messaging/routing").then(parseJsonResponse).then(function (r) {
        if (r && r._httpOk && r.ok !== false) { messagingState.routing = r; renderAutoReply(); }
      }).catch(function () {});
      return fetch("/api/messaging/inbox", { cache: "no-store" });
    }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) messagingState.inbox = j.items || [];
      if (renderNow || activeTab === "aihub" || activeTab === "messaging") renderMessaging();
    }).catch(function (e) {
      messagingState.loaded = false;
      messagingState.lastSend = { ok: false, error: (e && e.message) || "messaging unavailable" };
      if (renderNow || activeTab === "aihub" || activeTab === "messaging") renderMessaging();
    });
  }

  function messagingProviderConfigLine(p) {
    var cfg = p.config || {}, rows = [];
    if (cfg.server_url) rows.push("server_url: " + cfg.server_url);
    if (cfg.send_url) rows.push("send_url: " + cfg.send_url);
    if (cfg.send_path) rows.push("send_path: " + cfg.send_path);
    if (cfg.status_path) rows.push("status_path: " + cfg.status_path);
    if (cfg.password_env) rows.push("password env: " + cfg.password_env);
    if (cfg.auth_header_env) rows.push("token env: " + cfg.auth_header_env);
    if (!rows.length) rows.push("no bridge URL configured");
    return rows.join(" | ");
  }

  function renderMessagingConfig(st, providers) {
    var cfgEl = $("#messaging-config"); if (!cfgEl) return;
    var lines = ['<div class="messaging-config-path">Config: ' + esc(st.config_path || "config/messaging.json") + '</div>'];
    lines.push('<div class="messaging-config-path">Outbox: ' + esc(st.history_path || ".messaging_history/outbox.jsonl") + '</div>');
    lines.push('<div class="messaging-config-path">Inbox: ' + esc(st.inbox_path || ".messaging_history/inbox.jsonl") + '</div>');
    for (var i = 0; i < providers.length; i++) {
      var p = providers[i];
      lines.push('<div class="messaging-config-row"><b>' + esc(p.label || p.id) + '</b><span>' + esc(messagingProviderConfigLine(p)) + '</span></div>');
    }
    cfgEl.innerHTML = lines.join("");
  }

  function renderMessaging() {
    var statusEl = $("#messaging-status"), outEl = $("#messaging-output"), lastEl = $("#messaging-last"), sub = $("#messaging-sub");
    if (!statusEl || !outEl) return;
    var st = messagingState.status || {}, providers = st.providers || [], defaultProvider = st.default_provider || "debug";
    var cards = '<div class="messaging-provider-grid">';
    for (var i = 0; i < providers.length; i++) {
      var p = providers[i];
      cards += '<div class="messaging-provider ' + esc(p.status || "") + '"><b>' + esc(p.label || p.id) + '</b><span>' + esc(p.status || "unknown") + '</span><small>' + esc(p.description || "") + '</small></div>';
    }
    cards += '</div>';
    statusEl.innerHTML = '<div class="messaging-default">Default provider: <b>' + esc(defaultProvider) + '</b> | Inbound: <b>' + esc(st.inbox_count == null ? "0" : st.inbox_count) + '</b></div>' + cards;
    if (sub) sub.textContent = 'default provider: ' + defaultProvider + ' - real bridges require Real send';
    renderMessagingConfig(st, providers);
    var sel = $("#messaging-provider");
    if (sel && providers.length) {
      var current = sel.value || defaultProvider, opts = "";
      for (var j = 0; j < providers.length; j++) opts += '<option value="' + esc(providers[j].id) + '">' + esc(providers[j].label || providers[j].id) + '</option>';
      sel.innerHTML = opts; sel.value = current; if (!sel.value) sel.value = defaultProvider;
    }
    updateMessagingWarning();
    var items = messagingState.outbox || [];
    if (lastEl) {
      var last = (st.last_attempt || items[0] || null);
      lastEl.textContent = last ? (last.timestamp + ' - ' + last.provider + ' - ' + last.status + ' - ' + last.text_preview) : 'No message attempts yet.';
    }
    if (!items.length) outEl.textContent = "No local message history yet.";
    else outEl.textContent = items.slice(0, 12).map(function (it) {
      var err = it.error ? (" | " + it.error) : "";
      return [it.timestamp, it.provider, it.status, it.target || "-", it.text_preview || ""].join(" | ") + err;
    }).join("\n");
    renderAutoReply();
    renderInbox();
  }

  function renderAutoReply() {
    var box = $("#autoreply-status"); if (!box) return;
    var st = messagingState.autoReply || {}, cfg = st.config || {};
    var senders = (cfg.allowed_senders || []).join(", ");
    var lines = [];
    var persona = String(cfg.reply_persona || "ai").toLowerCase();
    var speaksAs = persona === "ai" ? "as Anton" : ("as " + persona);
    lines.push('<div class="messaging-config-row"><b>Status</b><span>' + (cfg.enabled ? ("ENABLED - answers others " + esc(speaksAs)) : "disabled") + '</span></div>');
    lines.push('<div class="messaging-config-row"><b>Speaks as</b><span>' + esc(persona === "ai" ? "Anton (its own persona)" : persona) + '</span></div>');
    var rt = messagingState.routing;
    if (rt) {
      var verdict = rt.mode === "active" ? "DECIDING" : (rt.mode === "shadow" ? "watching only (old rules decide)" : "off");
      lines.push('<div class="messaging-config-row"><b>Agentic routing</b><span>' + esc(rt.mode + " - " + verdict) + '</span></div>');
      lines.push('<div class="messaging-config-row"><b>Watcher vs rules</b><span>' +
        esc(rt.agrees + " agree / " + rt.disagrees + " differ of " + rt.decisions +
            "; " + rt.would_override_in_active + " confident enough to act") + '</span></div>');
    }
    lines.push('<div class="messaging-config-row"><b>Model</b><span>' + esc(cfg.model || "-") + '</span></div>');
    lines.push('<div class="messaging-config-row"><b>Allowed senders</b><span>' + esc(senders || "none (blocks everyone)") + '</span></div>');
    lines.push('<div class="messaging-config-row"><b>Replies last hour</b><span>' + esc(String(st.recent_replies_last_hour == null ? "-" : st.recent_replies_last_hour)) + '</span></div>');
    box.innerHTML = lines.join("");
    var en = $("#autoreply-enabled"); if (en) en.checked = !!cfg.enabled;
    var aen = $("#assistant-enabled"); if (aen) aen.checked = !!cfg.assistant_enabled;
    var cal = $("#calendar-enabled"); if (cal) cal.checked = !!cfg.calendar_enabled;
    var sendersEl = $("#autoreply-senders");
    if (sendersEl && document.activeElement !== sendersEl) sendersEl.value = senders;
    var modelEl = $("#autoreply-model");
    if (modelEl && document.activeElement !== modelEl) modelEl.value = cfg.model || "";
  }

  function renderInbox() {
    var el = $("#messaging-inbox"); if (!el) return;
    var items = messagingState.inbox || [];
    if (!items.length) { el.textContent = "No inbound messages yet."; return; }
    el.innerHTML = items.slice(0, 12).map(function (it) {
      var who = it.is_from_me ? "me" : (it.sender || "?");
      return '<div class="messaging-config-row"><b>' + esc(who) + '</b><span>' + esc((it.text || "").slice(0, 120) || "(no text)") + '</span></div>';
    }).join("");
  }

  function saveAutoReply() {
    var out = $("#autoreply-output");
    var senders = (($("#autoreply-senders") && $("#autoreply-senders").value) || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var body = {
      enabled: !!($("#autoreply-enabled") && $("#autoreply-enabled").checked),
      assistant_enabled: !!($("#assistant-enabled") && $("#assistant-enabled").checked),
      calendar_enabled: !!($("#calendar-enabled") && $("#calendar-enabled").checked),
      allowed_senders: senders
    };
    var model = (($("#autoreply-model") && $("#autoreply-model").value) || "").trim();
    if (model) body.model = model;
    fetch("/api/messaging/autoreply", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "save failed"));
      messagingState.autoReply = j;
      if (out) out.textContent = "Saved.";
      renderAutoReply();
    }).catch(function (e) {
      if (out) out.textContent = "FAILED: " + ((e && e.message) || "network error");
    });
  }

  function testAutoReply() {
    var out = $("#autoreply-output");
    var text = (($("#autoreply-test-text") && $("#autoreply-test-text").value) || "").trim();
    if (!text) { if (out) out.textContent = "Enter a test message first."; return; }
    if (out) out.textContent = "Generating (model may need to load, up to 2 min)...";
    var btn = $("#autoreply-test"); if (btn) btn.disabled = true;
    fetch("/api/messaging/autoreply/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: text })
    }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "test failed"));
      if (out) out.textContent = "AI reply (" + (j.model || "?") + "):\n" + (j.reply || "");
    }).catch(function (e) {
      if (out) out.textContent = "FAILED: " + ((e && e.message) || "network error");
    }).then(function () { if (btn) btn.disabled = false; });
  }

  function renderMessagingInit() {
    renderMessaging(); fetchMessagingStatus(true); fetchDigest(); fetchRigAgent();
    fetchCorrections(false);
  }

  function renderCorrections(j) {
    var el = $("#corrections-status");
    if (!el) return;
    if (!j) { el.textContent = "quality: unavailable"; return; }
    var rate = Number(j.override_rate || 0);
    var tone = rate >= 30 ? "red" : (rate >= 15 ? "amber" : "green");
    el.innerHTML =
      '<div><b style="color:var(--' + tone + ')">' + rate + '% of replies you rewrote</b>' +
      ' &middot; ' + esc(String(j.corrections || 0)) + ' correction examples from ' +
      esc(String(j.bot_replies_scanned || 0)) + ' replies</div>' +
      '<div class="messaging-config-path">' +
      (j.style_examples_enabled
        ? "fed back into every reply as style examples (corrections first)"
        : "style_examples is 0 — set it above 0 to actually use these") +
      '</div>';
  }

  function fetchCorrections(rebuild) {
    if (!$("#corrections-status")) return;
    var opts = rebuild
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
      : { cache: "no-store" };
    var url = rebuild ? "/api/messaging/corrections/mine" : "/api/messaging/corrections";
    if (rebuild) { var el = $("#corrections-status"); if (el) el.textContent = "scanning history..."; }
    fetch(url, opts).then(parseJsonResponse).then(function (j) {
      renderCorrections(j && j._httpOk && j.ok !== false ? j : null);
    }).catch(function () { renderCorrections(null); });
  }

  var rigAgentCfg = null;

  function renderRigAgent() {
    var st = $("#rig-agent-status");
    if (!st) return;
    if (!rigAgentCfg) { st.textContent = "loading..."; return; }
    var owners = rigAgentCfg.owners || [];
    st.innerHTML =
      '<div>' + (rigAgentCfg.enabled ? "Enabled" : "Disabled") + " &middot; " +
      (owners.length ? esc(owners.join(", "))
                     : "<b>no owners set — nobody can query the rig by message</b>") +
      '</div>' +
      '<div class="messaging-config-path">calendar sources: ' + (rigAgentCfg.calendar_urls || 0) +
      ' &middot; window ' + esc(String(rigAgentCfg.calendar_days || 7)) + ' days</div>';
    var chips = $("#rig-agent-skills");
    if (chips) {
      chips.innerHTML = (rigAgentCfg.skills || []).map(function (s) {
        return '<button class="digest-chip" type="button" data-rigskill="' + esc(s) + '">' + esc(s) + '</button>';
      }).join("");
    }
    var en = $("#rig-agent-enabled");
    if (en) en.checked = !!rigAgentCfg.enabled;
    var ow = $("#rig-agent-owners");
    if (ow && document.activeElement !== ow) ow.value = owners.join(", ");
    renderRigWatch();
  }

  function renderRigWatch() {
    var st = $("#rig-watch-status");
    if (!st || !rigAgentCfg) return;
    var w = rigAgentCfg.watch || {};
    var accounts = w.accounts || [];
    var mapped = w.mapped || {};
    // Name the real accounts: a typo'd account silently turns the feature off
    // for that person, which is the failure this panel exists to prevent.
    st.innerHTML =
      '<div>' + (w.enabled ? "On" : "Off") + " &middot; " +
      (w.server_ok ? "library reachable" : "<b>library unreachable</b>") +
      (w.error ? " &middot; " + esc(w.error) : "") + '</div>' +
      '<div class="messaging-config-path">accounts on the server: ' +
      (accounts.length ? esc(accounts.join(", ")) : "none found") +
      ' &middot; default: ' + esc(String(w.default_user || "-")) + '</div>';
    var en = $("#rig-watch-enabled");
    if (en) en.checked = !!w.enabled;
    var ta = $("#rig-watch-users");
    if (ta && document.activeElement !== ta) {
      ta.value = Object.keys(mapped).map(function (h) {
        return h + " = " + mapped[h];
      }).join("\n");
    }
  }

  function saveRigWatch() {
    var raw = (($("#rig-watch-users") && $("#rig-watch-users").value) || "");
    var map = {};
    raw.split(/\r?\n/).forEach(function (line) {
      var i = line.indexOf("=");
      if (i < 0) return;
      var h = line.slice(0, i).trim(), n = line.slice(i + 1).trim();
      if (h && n) map[h] = n;
    });
    var out = $("#rig-agent-output");
    fetch("/api/rig-agent", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jellyfin_users: map,
        watch_enabled: !!($("#rig-watch-enabled") && $("#rig-watch-enabled").checked)
      })
    }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) {
        rigAgentCfg = j; renderRigAgent();
        if (out) out.textContent = "Saved.";
      } else if (out) {
        out.textContent = "Save failed: " + ((j && j.error) || "?");
      }
    }).catch(function (e) { if (out) out.textContent = "Save failed: " + e; });
  }

  function fetchRigAgent() {
    if (!$("#rig-agent-status")) return;
    fetch("/api/rig-agent", { cache: "no-store" }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) { rigAgentCfg = j; renderRigAgent(); }
    }).catch(function () {});
  }

  function askRig(text) {
    var out = $("#rig-agent-output");
    if (!out) return;
    var q = String(text || "").trim();
    if (!q) return;
    out.textContent = "asking...";
    fetch("/api/rig-agent/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: q })
    }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "ask failed"));
      out.textContent = j.matched
        ? ("[" + j.skill + " · " + j.lang + "]\n" + j.text)
        : "Not recognised as a rig question. Try /status or 'cik gradi gpu?'";
    }).catch(function (e) {
      out.textContent = "FAILED: " + ((e && e.message) || "network error");
    });
  }

  function saveRigAgent() {
    var ics = (($("#rig-agent-ics") && $("#rig-agent-ics").value) || "")
      .split(/[\n,]/).map(function (s) { return s.trim(); }).filter(Boolean);
    var owners = (($("#rig-agent-owners") && $("#rig-agent-owners").value) || "")
      .split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var payload = { owners: owners, enabled: !!($("#rig-agent-enabled") && $("#rig-agent-enabled").checked) };
    if (ics.length) payload.calendar_ics = ics;
    var out = $("#rig-agent-output");
    fetch("/api/rig-agent", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "save failed"));
      rigAgentCfg = j; renderRigAgent();
      if (out) out.textContent = "Saved.";
    }).catch(function (e) {
      if (out) out.textContent = "FAILED: " + ((e && e.message) || "network error");
    });
  }

  var digestCfg = null;

  function digestRecipients() {
    var out = ((digestCfg && digestCfg.targets) || []).slice();
    var single = (digestCfg && digestCfg.target) || "";
    if (single && out.indexOf(single) < 0) out.push(single);
    return out;
  }

  function fetchDigest() {
    fetch("/api/messaging/digest", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.ok) { digestCfg = j.config || {}; renderDigest(j.last_sent); } })
      .catch(function () {});
    fetch("/api/messaging/inbox?limit=50", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var dl = $("#digest-known-chats");
        if (!dl || !j || !j.items) return;
        var seen = {};
        var html = "";
        for (var i = 0; i < j.items.length; i++) {
          var g = j.items[i].chat_guid;
          if (g && !seen[g]) { seen[g] = 1; html += '<option value="' + esc(g) + '"></option>'; }
        }
        dl.innerHTML = html;
      })
      .catch(function () {});
  }

  function renderDigest(lastSent) {
    if (!digestCfg) return;
    var st = $("#digest-status");
    if (st) st.textContent = (digestCfg.enabled ? "Enabled" : "Disabled") +
      " · daily after " + (digestCfg.hour != null ? digestCfg.hour : 8) + ":00" +
      (lastSent ? " · last sent " + lastSent : "");
    var en = $("#digest-enabled");
    if (en) en.checked = !!digestCfg.enabled;
    var hr = $("#digest-hour");
    if (hr && document.activeElement !== hr) hr.value = digestCfg.hour != null ? digestCfg.hour : 8;
    var box = $("#digest-recipients");
    if (box) {
      var recs = digestRecipients();
      var html = "";
      for (var i = 0; i < recs.length; i++) {
        html += '<span class="digest-chip">' + esc(recs[i]) +
          '<button class="digest-x" data-guid="' + esc(recs[i]) + '" type="button" aria-label="Remove">&times;</button></span>';
      }
      box.innerHTML = html || '<span class="messaging-note">No recipients yet — nobody gets the digest.</span>';
    }
  }

  function saveDigest(partial) {
    fetch("/api/messaging/digest-config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(partial)
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok) { digestCfg = j.config || {}; renderDigest(); }
        else { var o = $("#digest-output"); if (o) o.textContent = "FAILED: " + ((j && j.error) || "save failed"); }
      })
      .catch(function (e) { var o = $("#digest-output"); if (o) o.textContent = "FAILED: " + (e.message || "network"); });
  }

  function runDigest(dry) {
    var o = $("#digest-output");
    var btn = $(dry ? "#digest-preview" : "#digest-send");
    if (btn) btn.disabled = true;
    if (o) o.textContent = dry ? "Building preview (models may need a minute to load)..." : "Building and sending...";
    fetch("/api/messaging/digest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: !!dry })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (!o) return;
        if (j && j.ok) {
          o.textContent = (dry ? "" : "SENT: " + JSON.stringify(j.send_status || {}) + "\n\n") + (j.digest || "");
        } else {
          o.textContent = "FAILED: " + ((j && j.error) || "digest failed");
        }
        fetchDigest();
      })
      .catch(function (e) { if (o) o.textContent = "FAILED: " + (e.message || "network"); })
      .then(function () { if (btn) btn.disabled = false; });
  }

  function setMessagingBusy(b) {
    messagingState.busy = b;
    var btn = $("#messaging-send");
    if (btn) { btn.disabled = b; btn.textContent = b ? "Sending..." : "Send"; }
  }

  function updateMessagingWarning() {
    var provider = ($("#messaging-provider") && $("#messaging-provider").value) || "debug";
    var realSend = !!($("#messaging-real-send") && $("#messaging-real-send").checked);
    var warning = $("#messaging-warning"); if (!warning) return;
    if (provider === "debug") warning.textContent = "Debug records locally and sends nothing.";
    else if (!realSend) warning.textContent = "Real send is required for non-debug providers; backend will return 400.";
    else warning.textContent = "Real send requested; backend config and secrets are required.";
  }

  function sendMessagingTest() {
    if (messagingState.busy) return;
    var provider = ($("#messaging-provider") && $("#messaging-provider").value) || "debug";
    var target = ($("#messaging-target") && $("#messaging-target").value) || "";
    var text = ($("#messaging-text") && $("#messaging-text").value || "").trim();
    var realSend = !!($("#messaging-real-send") && $("#messaging-real-send").checked);
    var outEl = $("#messaging-output");
    if (!text) { if (outEl) outEl.textContent = "Message text is required."; return; }
    updateMessagingWarning(); setMessagingBusy(true);
    fetch("/api/messaging/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider, target: target, text: text, real_send: realSend, metadata: { source: "dashboard" } })
    }).then(parseJsonResponse).then(function (j) {
      messagingState.lastSend = j;
      if (outEl && (!j._httpOk || j.ok === false)) outEl.textContent = "FAILED: " + responseError(j, "send failed");
      fetchMessagingStatus(true);
    }).catch(function (e) {
      if (outEl) outEl.textContent = "FAILED: " + ((e && e.message) || "network error");
    }).then(function () { setMessagingBusy(false); });
  }

  function hubAction(model, act) {
    hubBusy[model] = act === "load" ? "Loading" : "Unloading";
    renderAihub();
    fetch("/api/ollama/" + act, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: model })
    }).then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function () {})
      .catch(function () {})
      .then(function () {
        hubBusy[model] = null;
        fetchHub();
      });
  }

  var hermesState = {
    status: null, items: [], stats: null, q: "", kindFilter: "", loaded: false,
    addBusy: false, reviewBusy: false, toggleBusy: false, sumBusy: false,
    addKind: "note", reviewProvider: "local", reviewOut: ""
  };
  var HERMES_KINDS = ["note", "preference", "person", "project", "correction", "rule", "fact"];
  var hermesSearchTimer = null;

  function hermesListURL() {
    return "/api/hermes?limit=100" +
      (hermesState.q ? "&q=" + encodeURIComponent(hermesState.q) : "") +
      (hermesState.kindFilter ? "&kind=" + encodeURIComponent(hermesState.kindFilter) : "");
  }

  function hermesBadge(status) {
    var s = String(status || "").toLowerCase();
    var cls = "off", label = status || "-";
    if (s === "ready") { cls = "on"; }
    else if (s === "missing_key") { cls = "off"; label = "no key · optional"; }
    else if (s === "not_ready") { cls = "warn"; label = "not set up"; }
    else if (s === "disabled") { cls = "off"; }
    return '<span class="hm-badge ' + cls + '">' + esc(label) + '</span>';
  }

  function fetchHermes(renderNow) {
    fetch("/api/ai/status", { cache: "no-store" }).then(parseJsonResponse).then(function (j) {
      hermesState.status = (j && j._httpOk) ? j : { ok: false, error: responseError(j, "ai status failed") };
    }).catch(function () {
      hermesState.status = { ok: false, error: "unreachable" };
    }).then(function () {
      return fetch(hermesListURL(), { cache: "no-store" }).then(parseJsonResponse);
    }).then(function (j) {
      if (j && j._httpOk && j.ok !== false) {
        hermesState.items = j.items || [];
        hermesState.stats = j.stats || null;
      }
      hermesState.loaded = true;
      if (renderNow || activeTab === "hermes") renderHermes();
    }).catch(function () {
      hermesState.loaded = true;
      if (renderNow || activeTab === "hermes") renderHermes();
    });
  }

  // List-only refresh (keeps the search box focused while typing).
  function refreshHermesList() {
    fetch(hermesListURL(), { cache: "no-store" }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) {
        hermesState.items = j.items || [];
        hermesState.stats = j.stats || null;
      }
      var listEl = $("#hermes-list");
      if (listEl) listEl.innerHTML = hermesListHTML(hermesState.items);
      var statEl = $("#hermes-stats");
      if (statEl) statEl.innerHTML = hermesStatsHTML(hermesState.stats);
      var kindsEl = $("#hermes-kinds");
      if (kindsEl) kindsEl.innerHTML = hermesKindChipsHTML();
    }).catch(function () {});
  }

  function aiStackCardHTML(st) {
    if (!st || st.ok === false) {
      return '<div class="gcard"><div class="card-h">AI Stack</div><div class="muted">' +
        esc((st && st.error) || "AI stack status unavailable") + '</div></div>';
    }
    var pol = st.policy || {}, models = st.models || {}, routes = st.routes || {};
    var rootBadge = st.local_root_exists ? hermesBadge("ready") : hermesBadge("not_ready");
    var polRows = rowsHTML([
      ["Autonomy", esc(safe(pol.autonomy))],
      ["Cloud", esc(safe(pol.cloud))],
      ["Redact cloud prompts", pol.redact_cloud_prompts ? "yes" : "no"],
      ["Local root", esc(safe(st.local_root)) + " " + rootBadge]
    ]);
    var modelRows = rowsHTML(Object.keys(models).map(function (k) {
      return [esc(k), esc(safe(models[k]))];
    }));
    var routeRows = rowsHTML(Object.keys(routes).map(function (k) {
      var r = routes[k] || {};
      var val = (r.provider || "") + (r.model ? " · " + r.model : "") + (r.fallback ? " (fb: " + r.fallback + ")" : "");
      return [esc(k), esc(val || "-")];
    }));
    var cloud = st.cloud_reviewers || [];
    var cloudRows = rowsHTML(cloud.map(function (c) {
      var model = c.model || (c.models && c.models.join(", ")) || "-";
      return [esc(c.id) + " " + hermesBadge(c.status), esc(String(model))];
    }));
    var bb = st.bluebubbles || {};
    var bbBadge = (bb.status === "ready")
      ? hermesBadge("ready")
      : '<span class="hm-badge off">optional · no Mac bridge</span>';
    var intRows = rowsHTML([
      ["BlueBubbles", bbBadge],
      ["Memory store", st.memory && st.memory.enabled ? hermesBadge("ready") : hermesBadge("disabled")]
    ]);
    return '<div class="gcard"><div class="card-h">AI Stack</div>' +
      '<div class="hm-sub">Policy</div>' + polRows +
      '<div class="hm-sub">Models</div>' + modelRows +
      '<div class="hm-sub">Routes</div>' + routeRows +
      '<div class="hm-sub">Cloud reviewers</div>' + (cloud.length ? cloudRows : '<div class="muted">none configured</div>') +
      '<div class="hm-sub">Integrations</div>' + intRows +
      '</div>';
  }

  function hermesStatsHTML(stats) {
    if (!stats) return '<span class="muted">memory stats unavailable</span>';
    var kinds = (stats.kinds || []).map(function (k) { return k.kind + " " + k.count; }).join(" · ");
    return '<b>' + (stats.count == null ? 0 : stats.count) + '</b> memories' +
      (kinds ? ' &middot; ' + esc(kinds) : '') +
      '<span class="hm-dbpath">' + esc(safe(stats.db_path)) + '</span>';
  }

  function hermesItemHTML(m) {
    var meta = [];
    if (m.source) meta.push(m.source);
    if (m.created_at) meta.push(String(m.created_at).slice(0, 10));
    return '<div class="hm-item' + (m.pinned ? ' pinned' : '') + '">' +
      '<div class="hm-item-top">' +
        '<span class="hm-kind">' + esc(safe(m.kind)) + '</span>' +
        '<span class="hm-title">' + esc(safe(m.title)) + '</span>' +
        '<button class="hm-pinbtn' + (m.pinned ? ' on' : '') + '" data-id="' + m.id +
          '" data-pinned="' + (m.pinned ? 1 : 0) + '" title="' + (m.pinned ? 'Unpin' : 'Pin') +
          '" aria-label="Pin">' + (m.pinned ? '&#9733;' : '&#9734;') + '</button>' +
        '<button class="hm-del" data-id="' + m.id + '" title="Delete" aria-label="Delete">&#10005;</button>' +
      '</div>' +
      '<div class="hm-content">' + esc(safe(m.content)) + '</div>' +
      (meta.length ? '<div class="hm-meta">' + esc(meta.join(" · ")) + '</div>' : '') +
    '</div>';
  }

  function hermesListHTML(items) {
    if (!items || !items.length) {
      return '<div class="muted">No memories yet. Add durable preferences, project facts, corrections, or rules above.</div>';
    }
    var people = [], rest = [], i;
    for (i = 0; i < items.length; i++) {
      (items[i].kind === "person" ? people : rest).push(items[i]);
    }
    var h = '';
    if (people.length) {
      people.sort(function (a, b) {
        return String(a.title).localeCompare(String(b.title)) ||
               String(b.created_at || "").localeCompare(String(a.created_at || ""));
      });
      h += '<div class="hm-sub">Contacts (auto-learned from chats)</div>';
      for (i = 0; i < people.length; i++) h += hermesItemHTML(people[i]);
      if (rest.length) h += '<div class="hm-sub">Rules &amp; notes</div>';
    }
    for (i = 0; i < rest.length; i++) h += hermesItemHTML(rest[i]);
    return h;
  }

  function hermesKindChipsHTML() {
    var kinds = (hermesState.stats && hermesState.stats.kinds) || [];
    if (!kinds.length) return '';
    var h = '<button class="term-chip' + (!hermesState.kindFilter ? ' on' : '') + '" type="button" data-hkind="">all</button>';
    for (var i = 0; i < kinds.length; i++) {
      var k = kinds[i];
      h += '<button class="term-chip' + (hermesState.kindFilter === k.kind ? ' on' : '') +
        '" type="button" data-hkind="' + esc(k.kind) + '">' + esc(k.kind) + ' ' + k.count + '</button>';
    }
    return h;
  }

  function memoryCardHTML() {
    var enabled = hermesState.stats ? hermesState.stats.enabled : true;
    var togLabel = hermesState.toggleBusy ? "..." : (enabled ? "Enabled" : "Disabled");
    var opts = HERMES_KINDS.map(function (k) {
      return '<option value="' + k + '"' + (k === hermesState.addKind ? ' selected' : '') + '>' + k + '</option>';
    }).join("");
    return '<div class="gcard">' +
      '<div class="hm-head">' +
        '<div class="card-h">Hermes Memory</div>' +
        '<div class="hm-head-actions">' +
          '<button id="hermes-toggle" class="btn-sm ' + (enabled ? '' : 'btn-ghost') + '">' + togLabel + '</button>' +
          '<button id="hermes-summarize" class="btn-sm btn-ghost" type="button"' + (hermesState.sumBusy ? ' disabled' : '') + '>' +
            (hermesState.sumBusy ? 'Summarizing...' : 'Summarize') + '</button>' +
          '<button id="hermes-refresh" class="btn-sm btn-ghost" type="button">Refresh</button>' +
        '</div>' +
      '</div>' +
      '<div id="hermes-stats" class="hm-stats">' + hermesStatsHTML(hermesState.stats) + '</div>' +
      '<div id="hermes-kinds" class="hm-kinds">' + hermesKindChipsHTML() + '</div>' +
      '<div class="hm-add">' +
        '<div class="hm-add-row">' +
          '<select id="hermes-kind" class="chat-select hm-kind-sel">' + opts + '</select>' +
          '<input id="hermes-title" class="messaging-input" placeholder="Title (optional)" />' +
        '</div>' +
        '<textarea id="hermes-content" class="studio-prompt" rows="2" placeholder="What should the rig remember? (preference, project fact, correction, rule...)"></textarea>' +
        '<div class="hm-add-row hm-add-actions">' +
          '<label class="messaging-check"><input id="hermes-pin" type="checkbox" /><span>Pin</span></label>' +
          '<button id="hermes-add" class="btn-sm"' + (hermesState.addBusy ? ' disabled' : '') + '>' + (hermesState.addBusy ? 'Saving...' : 'Save memory') + '</button>' +
        '</div>' +
      '</div>' +
      '<input id="hermes-search" class="messaging-input hm-search" placeholder="Search memories..." value="' + esc(hermesState.q) + '" />' +
      '<div id="hermes-list" class="hm-list">' + hermesListHTML(hermesState.items) + '</div>' +
      '</div>';
  }

  function reviewCardHTML() {
    function seg(v, label) {
      return '<button class="seg-btn' + (hermesState.reviewProvider === v ? ' active' : '') +
        '" type="button" data-rev-provider="' + v + '">' + label + '</button>';
    }
    return '<div class="gcard">' +
      '<div class="card-h">AI Review (Hermes-aware)</div>' +
      '<div class="seg hm-rev-seg" id="hermes-rev-seg">' +
        seg("local", "Local") + seg("anthropic", "Anthropic") + seg("openrouter", "OpenRouter") +
      '</div>' +
      '<input id="hermes-rev-subject" class="messaging-input" placeholder="Subject / context (optional)" />' +
      '<textarea id="hermes-rev-prompt" class="studio-prompt" rows="3" placeholder="Ask for a code/setup review. Local runs on Ollama; cloud is redacted and opt-in."></textarea>' +
      '<div class="hm-rev-row">' +
        '<span class="muted">Local uses Ollama on this rig and streams live. Cloud needs an API key and sends a redacted prompt. Results are saved to memory.</span>' +
        '<button id="hermes-review" class="btn-sm"' + (hermesState.reviewBusy ? ' disabled' : '') + '>' + (hermesState.reviewBusy ? 'Reviewing...' : 'Run review') + '</button>' +
      '</div>' +
      '<pre id="hermes-rev-out" class="hm-rev-out"' + (hermesState.reviewOut ? '' : ' style="display:none"') + '>' + esc(hermesState.reviewOut) + '</pre>' +
      '</div>';
  }

  function renderHermes() {
    var el = $("#hermes-card");
    if (!el) return;
    if (!hermesState.loaded && hermesState.status === null) {
      el.innerHTML = '<div class="stack">' + skeleton(6) + skeleton(5) + '</div>';
      return;
    }
    el.innerHTML = '<div class="stack">' +
      aiStackCardHTML(hermesState.status) +
      memoryCardHTML() +
      reviewCardHTML() +
      '</div>';
    wireHermesControls();
  }

  function wireHermesControls() {
    var add = $("#hermes-add");
    if (add) add.onclick = hermesAdd;
    var kind = $("#hermes-kind");
    if (kind) kind.onchange = function () { hermesState.addKind = this.value; };
    var tog = $("#hermes-toggle");
    if (tog) tog.onclick = hermesToggle;
    var summ = $("#hermes-summarize");
    if (summ) summ.onclick = hermesSummarize;
    var ref = $("#hermes-refresh");
    if (ref) ref.onclick = function () { fetchHermes(true); };
    var search = $("#hermes-search");
    if (search) search.oninput = function () {
      hermesState.q = this.value;
      if (hermesSearchTimer) clearTimeout(hermesSearchTimer);
      hermesSearchTimer = setTimeout(refreshHermesList, 220);
    };
    var rev = $("#hermes-review");
    if (rev) rev.onclick = hermesReview;
    var seg = $("#hermes-rev-seg");
    if (seg) seg.onclick = function (e) {
      var b = (e.target && e.target.closest) ? e.target.closest(".seg-btn[data-rev-provider]") : null;
      if (!b) return;
      hermesState.reviewProvider = b.getAttribute("data-rev-provider");
      var kids = seg.querySelectorAll(".seg-btn");
      for (var i = 0; i < kids.length; i++) kids[i].classList.toggle("active", kids[i] === b);
    };
  }

  function hermesAdd() {
    if (hermesState.addBusy) return;
    var content = ($("#hermes-content") && $("#hermes-content").value || "").trim();
    if (!content) { var c = $("#hermes-content"); if (c) c.focus(); return; }
    var payload = {
      kind: ($("#hermes-kind") && $("#hermes-kind").value) || "note",
      title: ($("#hermes-title") && $("#hermes-title").value || "").trim(),
      content: content,
      pinned: !!($("#hermes-pin") && $("#hermes-pin").checked),
      source: "dashboard"
    };
    hermesState.addBusy = true; renderHermes();
    fetch("/api/hermes", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    }).then(parseJsonResponse).then(function (j) {
      hermesState.addBusy = false;
      if (!j._httpOk || j.ok === false) { alert(responseError(j, "could not save memory")); renderHermes(); return; }
      fetchHermes(true);
    }).catch(function () { hermesState.addBusy = false; renderHermes(); });
  }

  function hermesDelete(id) {
    fetch("/api/hermes/" + id, { method: "DELETE" }).then(parseJsonResponse).then(function () {
      refreshHermesList();
    }).catch(function () {});
  }

  function hermesPinToggle(id, pinned) {
    fetch("/api/hermes/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned: !pinned })
    }).then(parseJsonResponse).then(function () { refreshHermesList(); }).catch(function () {});
  }

  // Distill all memories into one pinned summary via the local reviewer model.
  function hermesSummarize() {
    if (hermesState.sumBusy) return;
    hermesState.sumBusy = true; renderHermes();
    fetch("/api/hermes/summarize", { method: "POST" }).then(parseJsonResponse).then(function (j) {
      hermesState.sumBusy = false;
      if (!j._httpOk || j.ok === false) {
        hermesState.reviewOut = "Summarize failed: " + responseError(j, "error");
      } else {
        hermesState.reviewOut = (j.output || "(empty)") + "\n\n— summary saved to memory (pinned)";
      }
      fetchHermes(true);
    }).catch(function (e) {
      hermesState.sumBusy = false;
      hermesState.reviewOut = "Summarize failed: " + ((e && e.message) || "network error");
      renderHermes();
    });
  }

  function hermesToggle() {
    if (hermesState.toggleBusy) return;
    var cur = hermesState.stats ? hermesState.stats.enabled : true;
    hermesState.toggleBusy = true; renderHermes();
    fetch("/api/hermes/toggle", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !cur })
    }).then(parseJsonResponse).then(function () {
      hermesState.toggleBusy = false; fetchHermes(true);
    }).catch(function () { hermesState.toggleBusy = false; renderHermes(); });
  }

  function hermesReview() {
    if (hermesState.reviewBusy) return;
    var prompt = ($("#hermes-rev-prompt") && $("#hermes-rev-prompt").value || "").trim();
    if (!prompt) { var p = $("#hermes-rev-prompt"); if (p) p.focus(); return; }
    var subject = ($("#hermes-rev-subject") && $("#hermes-rev-subject").value || "").trim();
    var provider = hermesState.reviewProvider || "local";
    hermesState.reviewBusy = true; hermesState.reviewOut = ""; renderHermes();
    if (provider === "local") { hermesReviewStream(prompt, subject); return; }
    // Cloud providers: single redacted, non-streaming call.
    var payload = { prompt: prompt, subject: subject, provider: provider, allow_cloud: true };
    fetch("/api/ai/review", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    }).then(parseJsonResponse).then(function (j) {
      hermesState.reviewBusy = false;
      if (!j._httpOk || j.ok === false) {
        hermesState.reviewOut = "Error: " + responseError(j, "review failed");
      } else {
        hermesState.reviewOut = (j.output || "(empty response)") +
          "\n\n— " + (j.provider || provider) + (j.model ? " / " + j.model : "") + (j.redacted ? " (redacted)" : "");
      }
      fetchHermes(true);
    }).catch(function (e) {
      hermesState.reviewBusy = false;
      hermesState.reviewOut = "Error: " + (e && e.message ? e.message : "network error");
      renderHermes();
    });
  }

  // Token-streamed local review into #hermes-rev-out (no full re-render per token).
  function hermesReviewStream(prompt, subject) {
    var out = $("#hermes-rev-out");
    if (out) { out.style.display = ""; out.textContent = ""; out.classList.add("streaming"); }
    var acc = "", model = "";
    fetch("/api/ai/review/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: prompt, subject: subject })
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("review failed (" + resp.status + ")");
      var reader = resp.body.getReader(), decoder = new TextDecoder(), buf = "";
      function consume(line) {
        var ln = line.trim(); if (!ln) return;
        var o; try { o = JSON.parse(ln); } catch (e) { return; }
        if (o.type === "meta") { model = o.model || ""; }
        else if (o.type === "token") { acc += o.text || ""; if (out) { out.textContent = acc; out.scrollTop = out.scrollHeight; } }
        else if (o.type === "error") { acc += "\n[error] " + (o.error || ""); }
      }
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { buf += decoder.decode(); if (buf.trim()) consume(buf); return; }
          buf += decoder.decode(res.value, { stream: true });
          var parts = buf.split("\n"); buf = parts.pop();
          for (var i = 0; i < parts.length; i++) consume(parts[i]);
          return pump();
        });
      }
      return pump();
    }).then(function () {
      hermesState.reviewBusy = false;
      hermesState.reviewOut = (acc.trim() || "(empty response)") + (model ? "\n\n— local / " + model : "");
      renderHermes();
      fetchHermes(true);
    }).catch(function (e) {
      hermesState.reviewBusy = false;
      hermesState.reviewOut = (acc.trim() ? acc + "\n\n" : "") + "Error: " + (e && e.message ? e.message : "network error");
      renderHermes();
    });
  }

  var mediaState = { out: null, t: 0 };
  function fetchMedia() {
    fetch("/api/remote/status", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { mediaState.out = j; mediaState.t = Date.now(); if (activeTab === "media") renderMedia(); })
      .catch(function () { mediaState.out = { ok: false, error: "unreachable" }; if (activeTab === "media") renderMedia(); });
  }

  function renderMedia() {
    var el = $("#media-card");
    if (!el) return;
    var out = mediaState.out;
    if (!out) { el.innerHTML = '<div class="stack">' + skeleton(3) + skeleton(4) + '</div>'; return; }
    if (!out.ok) {
      el.innerHTML = '<div class="gcard"><div class="card-h">Media Console</div>' +
        '<div class="muted">Offline: ' + esc(out.error || "unreachable") + '<br>' +
        '<a class="mlink" href="' + esc(SERVICE_URL.media) + '" target="_blank" rel="noopener">Open directly &rarr;</a></div></div>';
      return;
    }
    var d = out.data || {};
    var jf = d.jellyfin || {}, counts = jf.counts || {};
    var media = d.media || {}, gpu = d.gpu || {}, cpu = d.cpu || {};

    var jh = '<div class="gcard"><div class="card-h">Jellyfin' +
      (jf.version ? ' &middot; v' + esc(jf.version) : '') + '</div>' +
      '<div class="lib-grid">' +
        libCell("Movies", counts.Movies) +
        libCell("Series", counts.Series) +
        libCell("Episodes", counts.Episodes) +
        libCell("Albums", counts.Albums) +
        libCell("Songs", counts.Songs) +
        libCell("Streaming", jf.stream_count != null ? jf.stream_count : (jf.counts ? 0 : "-")) +
      '</div>' +
      '<a class="mlink" href="' + esc(SERVICE_URL.jellyfin) + '" target="_blank" rel="noopener">Open Jellyfin &rarr;</a>' +
    '</div>';

    var disksH = '';
    var rdisks = d.disks || [];
    if (rdisks.length) {
      disksH = '<div class="gcard"><div class="card-h">Media Box Storage</div>';
      for (var x = 0; x < rdisks.length; x++) {
        var rd = rdisks[x];
        disksH += '<div class="row"><span class="k">' + esc(rd.label || rd.mount) + '</span>' +
          '<span class="v">' + gb(rd.used_gb) + " / " + gb(rd.total_gb) + '</span></div>' +
          bar(rd.used_pct, 80, 92);
      }
      disksH += '</div>';
    }

    var statH = '<div class="gcard"><div class="card-h">Media Box Vitals</div>' +
      rowsHTML([
        ["CPU", (cpu.load_pct != null ? num(cpu.load_pct) + "%" : "-") + "  ·  " + (cpu.temp != null ? Math.round(cpu.temp) + "°C" : "-")],
        ["GPU", (gpu.util != null ? gpu.util + "%" : "-") + "  ·  " + (gpu.temp != null ? Math.round(gpu.temp) + "°C" : "-") + "  ·  " + safe(gpu.clock)],
        ["Uptime", d.uptime_hours != null ? num(d.uptime_hours) + " h" : "-"]
      ]) + '</div>';

    el.innerHTML = '<div class="stack">' + jh + disksH + statH + '</div>';
  }
  function libCell(label, val) {
    return '<div class="lib-cell"><div class="lib-n">' + (val == null ? "-" : esc(val)) +
      '</div><div class="lib-l">' + label + '</div></div>';
  }

  var chat = { model: null, messages: [], streaming: false, controller: null, runId: 0 };
  var assistantMem = (function () {
    try { return localStorage.getItem("rigconsole.mem") === "1"; } catch (e) { return false; }
  })();
  var autoSpeak = (function () {
    try { return localStorage.getItem("rigconsole.voice") === "1"; } catch (e) { return false; }
  })();
  var chatAudio = null; // currently-playing voice clip (so a new one stops it)
  var CBID = 0;
  var CHAT_KEY = "rigconsole.chat";
  var CHAT_UNC_KEY = "rigconsole.uncensored.migrated";
  var CHAT_CAP = 40; // max messages persisted

  function populateChatModels() {
    var sel = $("#chat-model");
    if (!sel) return;
    var models = ecoState.models || [];
    // Prefer a non-coder talk model as default; fall back to first.
    var prev = chat.model || sel.value;
    var opts = "";
    var preferred = null;
    for (var i = 0; i < models.length; i++) {
      var n = models[i].name;
      opts += '<option value="' + esc(n) + '">' + esc(n) + '</option>';
      if (/^qwen/i.test(n) && !/coder/i.test(n)) preferred = n;
      else if (!/^qwen/i.test(preferred || "") && /^assistant-uncensored\b/i.test(n)) preferred = n;
      else if (/^assistant\b/i.test(n) && !preferred) preferred = n;
      else if (!preferred && /gemma|llama|mistral/i.test(n) && !/coder/i.test(n)) preferred = n;
    }
    sel.innerHTML = opts || '<option value="">no models</option>';

    // A saved pick normally wins, but two cases must override it, or the
    // dropdown silently sticks on something stale:
    var have = {};
    for (var k = 0; k < models.length; k++) have[models[k].name] = true;
    if (prev && !have[prev]) prev = null;
    try {
      // One-time migration: someone whose saved model is the OLD default has
      // not actually chosen it over the uncensored build -- they never saw it.
      // Move them across once, then respect every choice they make after.
      if (prev && /^assistant:/i.test(prev) && preferred && /uncensored/i.test(preferred)
          && !localStorage.getItem(CHAT_UNC_KEY)) {
        localStorage.setItem(CHAT_UNC_KEY, "1");
        prev = null;
      }
    } catch (e) { /* private mode: just keep the saved pick */ }

    var want = prev || preferred || (models[0] && models[0].name) || "";
    sel.value = want;
    chat.model = sel.value;
    updateModelBadge();
  }

  // Warm/cold chip beside the model picker (is the model resident in VRAM?).
  function updateModelBadge() {
    var b = $("#chat-warm");
    if (!b) return;
    if (!chat.model) { b.style.display = "none"; return; }
    var loaded = ecoState.modelLoaded || [];
    var isWarm = false;
    for (var i = 0; i < loaded.length; i++) { if (loaded[i].name === chat.model) { isWarm = true; break; } }
    b.style.display = "";
    b.className = "chat-warm " + (isWarm ? "warm" : "cold");
    b.textContent = isWarm ? "in VRAM" : "cold ~60s";

    // Uncensored builds get an unmistakable marker — you should never be unsure
    // which brain you are talking to.
    var u = $("#chat-unc");
    if (u) {
      var unc = /uncensored|abliterated|dolphin/i.test(chat.model);
      u.style.display = unc ? "" : "none";
      u.title = unc ? "Abliterated model — it will not refuse or lecture. Local only."
                    : "";
    }
  }

  // ---------- dependency-free markdown ----------
  function mdInline(s) {
    // s must already be HTML-escaped.
    s = s.replace(/`([^`]+)`/g, '<code class="md-ic">$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
                  '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return s;
  }
  function codeBlock(code, lang) {
    var id = "cb" + (CBID++);
    return '<div class="md-code"><div class="md-code-top"><span class="md-lang">' +
      esc(lang || "code") + '</span><button class="md-copy" type="button" data-cb="' + id +
      '">Copy</button></div><pre id="' + id + '"><code>' + esc(code) + '</code></pre></div>';
  }
  function renderMarkdown(text) {
    var lines = String(text).split("\n");
    var out = [], i, inCode = false, codeBuf = [], codeLang = "";
    var listType = null, listBuf = [];
    function flushList() {
      if (listType) { out.push('<' + listType + ' class="md-list">' + listBuf.join("") + '</' + listType + '>'); listBuf = []; listType = null; }
    }
    for (i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var fence = ln.match(/^```([^\s`]*)\s*$/);
      if (fence) {
        if (!inCode) { flushList(); inCode = true; codeLang = fence[1] || ""; codeBuf = []; }
        else { out.push(codeBlock(codeBuf.join("\n"), codeLang)); inCode = false; }
        continue;
      }
      if (inCode) { codeBuf.push(ln); continue; }
      var hm = ln.match(/^(#{1,4})\s+(.*)$/);
      if (hm) { flushList(); out.push('<div class="md-h md-h' + hm[1].length + '">' + mdInline(esc(hm[2])) + '</div>'); continue; }
      var um = ln.match(/^\s*[-*]\s+(.*)$/);
      if (um) { if (listType && listType !== "ul") flushList(); listType = "ul"; listBuf.push('<li>' + mdInline(esc(um[1])) + '</li>'); continue; }
      var om = ln.match(/^\s*\d+\.\s+(.*)$/);
      if (om) { if (listType && listType !== "ol") flushList(); listType = "ol"; listBuf.push('<li>' + mdInline(esc(om[1])) + '</li>'); continue; }
      if (!ln.trim()) { flushList(); continue; }
      flushList();
      out.push('<div class="md-p">' + mdInline(esc(ln)) + '</div>');
    }
    if (inCode) out.push(codeBlock(codeBuf.join("\n"), codeLang)); // unterminated = still streaming
    flushList();
    return out.join("");
  }

  // ---------- message DOM ----------
  function hideEmpty() { var e = $("#chat-empty"); if (e) e.style.display = "none"; }
  function scrollLog() { var l = $("#chat-log"); if (l) l.scrollTop = l.scrollHeight; }
  function showEmpty() {
    var log = $("#chat-log");
    if (!log) return;
    var chips = "";
    for (var i = 0; i < CHAT_STARTERS.length; i++) {
      chips += '<button class="ce-chip" type="button" data-starter="' + i + '">' +
               esc(CHAT_STARTERS[i]) + '</button>';
    }
    log.innerHTML = '<div class="chat-empty" id="chat-empty">' +
      '<div class="ce-big">Local Assistant</div>' +
      '<div class="ce-sub">Runs fully on this rig &middot; nothing leaves your network</div>' +
      '<div class="ce-chips">' + chips + '</div></div>';
    log.onclick = function (e) {
      var b = (e.target && e.target.closest) ? e.target.closest("[data-starter]") : null;
      if (!b) return;
      var input = $("#chat-input");
      if (!input) return;
      input.value = CHAT_STARTERS[parseInt(b.getAttribute("data-starter"), 10)] + " ";
      input.focus();
    };
  }
  function appendUserMsg(text) {
    var log = $("#chat-log");
    hideEmpty();
    var div = document.createElement("div");
    div.className = "msg user";
    div.innerHTML = '<div class="msg-role">You</div><div class="msg-body"></div>';
    log.appendChild(div);
    div.querySelector(".msg-body").textContent = text;
    scrollLog();
  }
  // Streaming assistant bubble with a thinking indicator + collapsible reasoning.
  function appendAssistantMsg() {
    var log = $("#chat-log");
    hideEmpty();
    var div = document.createElement("div");
    div.className = "msg assistant thinking";
    div.innerHTML =
      '<div class="msg-role">Assistant</div>' +
      '<div class="msg-think" style="display:none">' +
        '<button class="think-toggle" type="button">&#9656; Reasoning</button>' +
        '<div class="think-body" style="display:none"></div>' +
      '</div>' +
      '<div class="msg-body"><span class="think-dots">Thinking</span></div>';
    log.appendChild(div);
    scrollLog();
    return {
      root: div,
      body: div.querySelector(".msg-body"),
      think: div.querySelector(".msg-think"),
      thinkBody: div.querySelector(".think-body")
    };
  }
  function renderStoredMessages() {
    var log = $("#chat-log");
    if (!log) return;
    if (!chat.messages.length) { showEmpty(); return; }
    log.innerHTML = "";
    for (var i = 0; i < chat.messages.length; i++) {
      var m = chat.messages[i];
      if (m.role === "user") { appendUserMsg(m.content); continue; }
      var h = appendAssistantMsg();
      h.root.classList.remove("thinking");
      h.body.innerHTML = renderMarkdown(m.content);
      if (m.thinking) { h.think.style.display = ""; h.thinkBody.textContent = m.thinking; }
      addSaveToMemory(h.root, m.content);
    }
  }

  // ---------- persistence ----------
  function saveChat() {
    try {
      localStorage.setItem(CHAT_KEY, JSON.stringify({
        model: chat.model, messages: chat.messages.slice(-CHAT_CAP)
      }));
    } catch (e) {}
  }
  function loadChat() {
    try {
      var raw = localStorage.getItem(CHAT_KEY);
      if (!raw) return;
      var obj = JSON.parse(raw);
      if (obj && obj.messages) { chat.messages = obj.messages; if (obj.model) chat.model = obj.model; }
    } catch (e) {}
  }

  // ---------- send / stream ----------
  function mergeChatEvent(obj, acc) {
    if (!obj || typeof obj !== "object") return null;
    if (obj.error) return String(obj.error);
    if (obj.message) {
      if (obj.message.thinking) acc.thinking += obj.message.thinking;
      if (obj.message.content) acc.content += obj.message.content;
    }
    if (obj.done_reason) acc.doneReason = obj.done_reason;
    // Ollama puts its timing counters (nanoseconds) on the final frame. Worth
    // surfacing on a local rig — tok/s is the number you actually feel.
    if (obj.eval_count != null) acc.evalCount = obj.eval_count;
    if (obj.eval_duration != null) acc.evalDuration = obj.eval_duration;
    if (obj.prompt_eval_count != null) acc.promptCount = obj.prompt_eval_count;
    if (obj.load_duration != null) acc.loadDuration = obj.load_duration;
    return null;
  }

  function chatStatsHTML(acc) {
    var bits = [];
    if (acc.evalCount && acc.evalDuration) {
      bits.push(Math.round(acc.evalCount / (acc.evalDuration / 1e9)) + " tok/s");
    }
    if (acc.evalCount) bits.push(acc.evalCount + " tokens");
    if (acc.ttft) bits.push((acc.ttft / 1000).toFixed(1) + "s to first token");
    if (acc.loadDuration && acc.loadDuration > 1e9) {
      bits.push("+" + (acc.loadDuration / 1e9).toFixed(0) + "s model load");
    }
    if (acc.promptCount) bits.push(acc.promptCount + " prompt");
    if (!bits.length) return "";
    return '<div class="msg-stats">' + esc(bits.join(" · ")) + '</div>';
  }

  function friendlyChatError(message) {
    var m = String(message || "");
    if (/timed out|timeout/i.test(m)) return "Ollama took too long to respond. The model may still be loading.";
    if (/refused|unreachable|failed to fetch|network|urlopen|connect/i.test(m)) {
      return "Ollama is unavailable. Check the Ollama service, then retry.";
    }
    if (/not found/i.test(m)) return "That model is not available in Ollama. Select another model and retry.";
    return "The assistant request failed. Retry, or check Ollama in AI Hub.";
  }

  function sendChat() {
    if (chat.streaming) return;
    var input = $("#chat-input");
    var text = (input.value || "").trim();
    if (!text) return;
    var sel = $("#chat-model");
    chat.model = sel ? sel.value : chat.model;
    if (!chat.model) {
      var h0 = appendAssistantMsg(); h0.root.classList.remove("thinking");
      h0.body.innerHTML = '<div class="md-err">No model selected. Pull one in the AI Hub.</div>';
      return;
    }
    chat.messages.push({ role: "user", content: text });
    appendUserMsg(text);
    input.value = "";
    input.style.height = "auto";
    saveChat();
    maybeInjectThenStream(text);
  }

  // When the Memory toggle is on, pull relevant Hermes memories and inject them
  // as a system message (not stored in history) so the local model "remembers".
  function maybeInjectThenStream(queryText) {
    if (!assistantMem) { streamAssistant("", 0); return; }
    fetch("/api/hermes/context?limit=6&q=" + encodeURIComponent(queryText || ""), { cache: "no-store" })
      .then(parseJsonResponse).then(function (j) {
        var t = (j && j._httpOk && j.text) ? j.text : "";
        var n = (j && j.count) ? j.count : 0;
        streamAssistant(t, n);
      }).catch(function () { streamAssistant("", 0); });
  }

  // Streams a reply for the current chat.messages tail into a fresh bubble.
  function streamAssistant(memText, memCount) {
    var h = appendAssistantMsg();
    if (memText) {
      var note = document.createElement("div");
      note.className = "mem-inject";
      note.innerHTML = "&#129504; drawing on " + (memCount || 0) + " saved " + ((memCount === 1) ? "memory" : "memories");
      h.root.insertBefore(note, h.body);
    }
    var sendMsgs = memText
      ? [{ role: "system", content: memText }].concat(chat.messages)
      : chat.messages.slice();
    var runId = ++chat.runId;
    chat.streaming = true;
    setSendState(true);
    chat.controller = new AbortController();

    var acc = { content: "", thinking: "", doneReason: "" };
    var started = Date.now();
    var buf = "", rafPending = false;
    function flushRender() {
      rafPending = false;
      if (acc.thinking) { h.think.style.display = ""; h.thinkBody.textContent = acc.thinking; }
      if (acc.content) {
        if (!acc.ttft) acc.ttft = Date.now() - started;
        h.root.classList.remove("thinking");
        h.body.innerHTML = renderMarkdown(acc.content) + '<span class="md-caret"></span>';
      }
      scrollLog();
    }
    function scheduleRender() {
      if (rafPending) return;
      rafPending = true;
      (window.requestAnimationFrame || function (f) { setTimeout(f, 16); })(flushRender);
    }

    fetch("/api/ollama/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // persona:true -> the server prepends config/assistant_persona.md (ANTONS),
      // so the dashboard assistant has the same character as the iMessage one
      body: JSON.stringify({ model: chat.model, messages: sendMsgs, persona: true }),
      signal: chat.controller.signal
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("chat failed (" + resp.status + ")");
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      function consumeLine(line) {
        var ln = line.trim();
        if (!ln) return;
        var obj;
        try { obj = JSON.parse(ln); } catch (e) { return; }
        var streamError = mergeChatEvent(obj, acc);
        if (streamError) throw new Error(streamError);
      }
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            buf += decoder.decode();
            if (buf.trim()) consumeLine(buf);
            flushRender();
            finishChat(h, acc.content, acc.thinking, false, runId, acc);
            return;
          }
          buf += decoder.decode(res.value, { stream: true });
          var parts = buf.split("\n");
          buf = parts.pop();
          for (var i = 0; i < parts.length; i++) consumeLine(parts[i]);
          scheduleRender();
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      if (runId !== chat.runId) return;
      if (e && e.name === "AbortError") {
        flushRender();
        finishChat(h, acc.content, acc.thinking, true, runId, acc);
        return;
      }
      h.root.classList.remove("thinking");
      h.body.innerHTML = (acc.content ? renderMarkdown(acc.content) : "") +
        '<div class="md-err">' + esc(friendlyChatError(e.message)) +
        '</div><button class="retry-btn" type="button">Retry</button>';
      chat.streaming = false; chat.controller = null; setSendState(false);
    });
  }

  function finishChat(h, content, thinking, aborted, runId, acc) {
    if (runId !== chat.runId) return;
    h.root.classList.remove("thinking");
    if (!content) {
      h.body.innerHTML = aborted
        ? '<div class="muted chat-stopped">Stopped.</div>'
        : '<div class="md-err">The model returned no response.</div><button class="retry-btn" type="button">Retry</button>';
    } else {
      // drop the streaming caret, then add the perf readout
      h.body.innerHTML = renderMarkdown(content) + (acc ? chatStatsHTML(acc) : "");
      chat.messages.push({ role: "assistant", content: content, thinking: thinking || "" });
      addSaveToMemory(h.root, content);
      addCopyButton(h.root, content);
      if (autoSpeak && !aborted) speakText(content, null);
    }
    chat.streaming = false; chat.controller = null;
    setSendState(false);
    saveChat();
    fetchHub(); // refresh loaded-model state (+ warm badge)
  }

  // "Save to memory" affordance under a completed assistant reply.
  function addSaveToMemory(root, content) {
    if (!root || !content) return;
    var actions = document.createElement("div");
    actions.className = "msg-actions";
    var sv = document.createElement("button");
    sv.className = "msg-save"; sv.type = "button";
    sv.innerHTML = "&#129504; Save to memory";
    sv.onclick = function () { saveAssistantMemory(content, sv); };
    actions.appendChild(sv);
    var sp = document.createElement("button");
    sp.className = "msg-save"; sp.type = "button";
    sp.innerHTML = "&#128266; Speak";
    sp.onclick = function () { speakText(content, sp); };
    actions.appendChild(sp);
    root.appendChild(actions);
  }

  function addCopyButton(root, content) {
    var actions = root ? root.querySelector(".msg-actions") : null;
    if (!actions) return;
    var cp = document.createElement("button");
    cp.className = "msg-save"; cp.type = "button";
    cp.innerHTML = "&#128203; Copy";
    cp.onclick = function () {
      var done = function () { cp.innerHTML = "&#10003; Copied"; setTimeout(function () { cp.innerHTML = "&#128203; Copy"; }, 1400); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(content).then(done, function () {});
      }
    };
    actions.appendChild(cp);
  }

  // Starter prompts on an empty chat — cheaper than staring at a blank box.
  var CHAT_STARTERS = [
    "What should I get done today?",
    "Explain this like I'm teaching my brother to code:",
    "Draft a short reply to this message:",
    "Poke holes in this plan:"
  ];

  function saveAssistantMemory(content, btn) {
    if (btn.classList.contains("saved")) return;
    var title = String(content).split("\n")[0].replace(/^#+\s*/, "").slice(0, 80) || "Assistant note";
    btn.innerHTML = "Saving...";
    fetch("/api/hermes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "note", title: title, content: content, source: "assistant" })
    }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk && j.ok !== false) { btn.innerHTML = "&#10003; Saved to Hermes"; btn.classList.add("saved"); }
      else { btn.innerHTML = "Save failed"; }
    }).catch(function () { btn.innerHTML = "Save failed"; });
  }

  function stopChat() { if (chat.controller) { try { chat.controller.abort(); } catch (e) {} } }

  // The send button doubles as Stop while a reply streams.
  function setSendState(streaming) {
    var b = $("#chat-send");
    if (!b) return;
    if (streaming) { b.classList.add("stop"); b.innerHTML = "&#9632;"; b.setAttribute("aria-label", "Stop"); }
    else { b.classList.remove("stop"); b.innerHTML = "&#10148;"; b.setAttribute("aria-label", "Send"); }
  }

  function serviceAction(id, act) {
    hubBusy["svc:" + id] = act === "start" ? "Starting" : (act === "stop" ? "Stopping" : "Restarting");
    svcErr[id] = null;
    if (activeTab === "aihub") renderAihub();
    if (activeTab === "studio") renderStudioStatus();
    fetch("/api/services/" + id + "/" + act, { method: "POST" })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (j) {
        hubBusy["svc:" + id] = null;
        svcErr[id] = (j && j.ok === false) ? (j.error || "action failed") : null;
      })
      .catch(function () { hubBusy["svc:" + id] = null; svcErr[id] = "network error"; })
      .then(function () {
        fetchHub();
        if (activeTab === "studio") renderStudioStatus();
      });
  }

  var STUDIO_HISTORY_KEY = "rigStudioHistory.v2";
  var STUDIO_SIZES = [
    { label: "Icon · 1024 × 1024", w: 1024, h: 1024 },
    { label: "Wide · 1344 × 768", w: 1344, h: 768 },
    { label: "Full HD · 1920 × 1080", w: 1920, h: 1080 },
    { label: "iPhone wallpaper · 1260 × 2736", w: 1260, h: 2736 },
    { label: "1440p monitor · 2560 × 1440", w: 2560, h: 1440 },
    { label: "MacBook 14\" · 3024 × 1964", w: 3024, h: 1964 }
  ];
  var STUDIO_QUALITY = [
    { id: "fast", label: "Fast" },
    { id: "good", label: "Good" },
    { id: "best", label: "Best" }
  ];
  var studio = { mode: "image", busy: false, pollId: 0, history: [], engines: null, enginesAt: 0, enginesLoading: false };

  function studioHasZimage() {
    return !!(studio.engines && (studio.engines.zimage || studio.engines.zimage_turbo));
  }

  function studioQualityEta(q) {
    var z = studioHasZimage();
    if (q === "fast") return z ? "~10 s" : "~15 s";
    if (q === "good") return z ? "~1 min" : "~30 s";
    return z ? "~2-4 min" : "~1 min";
  }

  function studioSyncLabels() {
    var s = $("#studio-size"), q = $("#studio-quality"), l = $("#studio-length");
    var sl = $("#studio-size-label"), ql = $("#studio-quality-label"), ll = $("#studio-length-label");
    if (s && sl) sl.textContent = (STUDIO_SIZES[Number(s.value)] || STUDIO_SIZES[0]).label;
    if (q && ql) {
      var qq = STUDIO_QUALITY[Number(q.value)] || STUDIO_QUALITY[1];
      ql.textContent = qq.label + " · " + studioQualityEta(qq.id);
    }
    if (l && ll) {
      var secs = Number(l.value) || 6;
      ll.textContent = secs + " s · ~" + Math.round(secs * 2.5) + " min render";
    }
  }

  function readImageFile(file, cb) {
    if (file.size > 25 * 1024 * 1024) { cb(null, "Image too large (max 25 MB)"); return; }
    var fr = new FileReader();
    fr.onerror = function () { cb(null, "Could not read " + file.name); };
    fr.onload = function () {
      var im = new Image();
      im.onload = function () { cb({ b64: fr.result, name: file.name, w: im.naturalWidth, h: im.naturalHeight }); };
      im.onerror = function () { cb(null, file.name + " is not a readable image (use JPG or PNG)"); };
      im.src = fr.result;
    };
    fr.readAsDataURL(file);
  }

  function studioLoadEngines() {
    if (studio.enginesLoading) return;
    studio.enginesLoading = true;
    fetch("/api/comfy/engines", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.ok) studio.engines = j; })
      .catch(function () {})
      .then(function () {
        studio.enginesLoading = false;
        studio.enginesAt = Date.now();
        studioSyncLabels();
      });
  }

  function comfyUp() {
    var svc = ecoState.services || [];
    for (var i = 0; i < svc.length; i++) if (svc[i].id === "comfyui") return !!svc[i].up;
    return false;
  }

  function renderStudioStatus() {
    var el = $("#studio-status");
    if (!el) return;
    var busyS = hubBusy["svc:comfyui"];
    if (comfyUp()) {
      el.className = "studio-status ok";
      el.innerHTML = '<span class="dot on"></span> ComfyUI online';
    } else {
      el.className = "studio-status warn";
      el.innerHTML = '<span class="dot off"></span> ComfyUI is off &middot; ' +
        (busyS ? esc(busyS) + '...' :
          '<button class="svc-ctl start" data-svc="comfyui" data-act="start">Start ComfyUI</button>');
    }
    var hint = $("#studio-hint");
    if (hint) {
      if (studio.mode === "video") {
        hint.textContent = "LTX HQ video · longer runs cost real time on the 3090";
      } else if (studio.mode === "edit") {
        hint.textContent = (studio.engines && !studio.engines.qwen_edit)
          ? "Edit model not installed yet (Qwen-Image-Edit 2511)"
          : "Describe the change · ~1-2 min per edit";
      } else {
        hint.textContent = studio.engines && !studioHasZimage()
          ? "Using SDXL fallback (Z-Image models not installed)"
          : (studioHasZimage() ? "Z-Image · Fast skips the negative prompt" : "");
      }
    }
    var iopts = $("#studio-image-options");
    var vopts = $("#studio-video-options");
    var eopts = $("#studio-edit-options");
    var neg = $("#studio-negative");
    var prompt = $("#studio-prompt");
    if (iopts) iopts.style.display = studio.mode === "image" ? "grid" : "none";
    if (vopts) vopts.style.display = studio.mode === "video" ? "grid" : "none";
    if (eopts) eopts.style.display = studio.mode === "edit" ? "grid" : "none";
    if (neg) neg.style.display = studio.mode === "image" ? "block" : "none";
    if (prompt) prompt.placeholder = studio.mode === "edit"
      ? 'e.g. "Add the person from the second photo standing on the right"'
      : "Describe what to generate...";
    if (!studio.enginesAt || Date.now() - studio.enginesAt > 30000) studioLoadEngines();
    studioSyncLabels();
  }

  function studioSetMode(mode) {
    studio.mode = mode;
    var seg = $("#studio-mode");
    if (seg) {
      var b = seg.querySelectorAll(".seg-btn");
      for (var i = 0; i < b.length; i++) b[i].classList.toggle("active", b[i].getAttribute("data-mode") === mode);
    }
    renderStudioStatus();
  }

  function numInput(id, fallback) {
    var el = $(id);
    var n = el ? Number(el.value) : NaN;
    return isFinite(n) ? n : fallback;
  }

  function studioBuildPayload(prompt) {
    var size = STUDIO_SIZES[numInput("#studio-size", 3)] || STUDIO_SIZES[3];
    var q = STUDIO_QUALITY[numInput("#studio-quality", 1)] || STUDIO_QUALITY[1];
    return {
      prompt: prompt,
      negative: (($("#studio-negative") && $("#studio-negative").value) || "").trim(),
      quality: q.id,
      width: size.w,
      height: size.h
    };
  }

  function setStudioBusy(b) {
    var go = $("#studio-go");
    if (go) { go.disabled = b; go.textContent = b ? "Working..." : "Generate"; }
  }

  function studioGenerate() {
    if (studio.busy) return;
    var promptEl = $("#studio-prompt");
    var res = $("#studio-result");
    var prompt = (promptEl && promptEl.value || "").trim();
    if (!prompt) return;
    if (!comfyUp()) { res.innerHTML = '<div class="md-err">Start ComfyUI first.</div>'; return; }
    var kind = studio.mode;
    if (kind === "edit") {
      var photoIn = $("#studio-photo");
      if (!photoIn || !photoIn.files || !photoIn.files.length) {
        res.innerHTML = '<div class="md-err">Pick a photo to edit first.</div>';
        return;
      }
      studio.busy = true;
      setStudioBusy(true);
      var myPollE = ++studio.pollId;
      res.innerHTML = '<div class="studio-prog"><span class="spin"></span>Reading photo...</div>';
      readImageFile(photoIn.files[0], function (p1, err1) {
        if (!p1) {
          studio.busy = false; setStudioBusy(false);
          if (myPollE === studio.pollId) res.innerHTML = '<div class="md-err">' + esc(err1 || "read failed") + '</div>';
          return;
        }
        var finish = function (p2, err2) {
          if (p2 === null && err2) {
            studio.busy = false; setStudioBusy(false);
            if (myPollE === studio.pollId) res.innerHTML = '<div class="md-err">' + esc(err2) + '</div>';
            return;
          }
          studio.editPayload = {
            prompt: prompt,
            photo_b64: p1.b64, photo_name: p1.name,
            width: p1.w, height: p1.h
          };
          if (p2) { studio.editPayload.ref_b64 = p2.b64; studio.editPayload.ref_name = p2.name; }
          studioRunPull(prompt, "edit", myPollE, 1, 1);
        };
        var refIn = $("#studio-ref");
        if (refIn && refIn.files && refIn.files.length) readImageFile(refIn.files[0], finish);
        else finish(null, null);
      });
      return;
    }
    studio.busy = true;
    var myPoll = ++studio.pollId;
    setStudioBusy(true);
    studioRunPull(prompt, kind, myPoll, 1, 1);
  }

  function studioRunPull(prompt, kind, myPoll, index, total) {
    if (myPoll !== studio.pollId) return;
    var res = $("#studio-result");
    var payload = kind === "image" ? studioBuildPayload(prompt)
      : (kind === "edit" ? studio.editPayload
        : { prompt: prompt, seconds: numInput("#studio-length", 6) });
    res.innerHTML = '<div class="studio-prog"><span class="spin"></span>' +
      (kind === "video"
        ? "Rendering " + payload.seconds + " s video - roughly " + Math.round(payload.seconds * 2.5) + " min on the 3090"
        : (kind === "edit" ? "Editing photo - about a minute or two..." : "Generating image...")) + '</div>';
    fetch("/api/comfy/" + kind, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok || !j.prompt_id) throw new Error(j.error || "submit failed");
        payload.seed = j.seed == null ? payload.seed : j.seed;
        payload.engine = j.engine || payload.engine;
        payload.quality = j.quality || payload.quality;
        payload.steps = j.steps == null ? payload.steps : j.steps;
        payload.cfg = j.cfg == null ? payload.cfg : j.cfg;
        payload.seconds = j.seconds == null ? payload.seconds : j.seconds;
        payload.sourceWidth = j.source_width || payload.width;
        payload.sourceHeight = j.source_height || payload.height;
        payload.checkpoint = j.checkpoint || payload.checkpoint;
        payload.sampler = j.sampler || payload.sampler;
        payload.scheduler = j.scheduler || payload.scheduler;
        pollStudio(j.prompt_id, kind, myPoll, prompt, payload, index, total);
      })
      .catch(function (e) {
        studio.busy = false; setStudioBusy(false);
        if (myPoll === studio.pollId) res.innerHTML = '<div class="md-err">' + esc(e.message) + '</div>';
      });
  }

  function pollStudio(pid, kind, myPoll, prompt, payload, index, total) {
    if (myPoll !== studio.pollId) return;
    fetch("/api/comfy/result/" + encodeURIComponent(pid), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (myPoll !== studio.pollId) return;
        if (j.status === "done" && j.files && j.files.length) {
          showStudioResult(j.files[j.files.length - 1], kind, prompt, payload, index, total);
          if (index < total) {
            studioRunPull(prompt, kind, myPoll, index + 1, total);
          } else {
            studio.busy = false; setStudioBusy(false);
          }
        } else if (j.status === "error") {
          studio.busy = false; setStudioBusy(false);
          $("#studio-result").innerHTML = '<div class="md-err">Generation failed in ComfyUI.</div>';
        } else {
          setTimeout(function () { pollStudio(pid, kind, myPoll, prompt, payload, index, total); }, kind === "video" ? 4000 : 1200);
        }
      })
      .catch(function () { setTimeout(function () { pollStudio(pid, kind, myPoll, prompt, payload, index, total); }, 3000); });
  }

  function studioFileUrl(f) {
    return f.artifact_url || ("/api/comfy/file?filename=" + encodeURIComponent(f.filename) +
      "&subfolder=" + encodeURIComponent(f.subfolder || "") +
      "&type=" + encodeURIComponent(f.type || "output"));
  }

  function studioLoadHistory() {
    try { studio.history = JSON.parse(localStorage.getItem(STUDIO_HISTORY_KEY) || "[]") || []; }
    catch (e) { studio.history = []; }
  }

  function studioSaveHistory() {
    try { localStorage.setItem(STUDIO_HISTORY_KEY, JSON.stringify(studio.history.slice(0, 18))); }
    catch (e) {}
  }

  function studioResultMeta(item) {
    var bits = [];
    if (item.engine) bits.push(item.engine);
    if (item.kind === "video" && item.seconds) bits.push(item.seconds + " s");
    if (item.width && item.height) bits.push(item.width + "x" + item.height);
    if (item.quality) bits.push(item.quality);
    if (item.steps) bits.push(item.steps + " steps");
    if (item.seed != null) bits.push("seed " + item.seed);
    return bits.join(" · ");
  }

  function studioAddHistory(item) {
    studio.history.unshift(item);
    studio.history = studio.history.slice(0, 18);
    studioSaveHistory();
    renderStudioHistory();
  }

  function renderStudioHistory() {
    var last = $("#studio-last");
    var hist = $("#studio-history");
    if (!last || !hist) return;
    if (!studio.history.length) {
      last.innerHTML = "";
      hist.innerHTML = "";
      return;
    }
    var h = studio.history[0];
    last.innerHTML = '<div class="studio-last-card">' +
      '<span>Last created</span><strong>' + esc(h.created) + '</strong><em>' + esc(studioResultMeta(h)) + '</em>' +
      '</div>';
    var cards = "";
    for (var i = 0; i < Math.min(studio.history.length, 8); i++) {
      var it = studio.history[i];
      var media = it.kind === "video"
        ? '<video src="' + esc(it.src) + '" muted playsinline></video>'
        : '<img src="' + esc(it.src) + '" alt="studio result">';
      cards += '<a class="studio-history-card" href="' + esc(it.src) + '" download>' +
        media + '<span>' + esc(it.created) + '</span><em>' + esc(studioResultMeta(it)) + '</em></a>';
    }
    hist.innerHTML = cards;
  }

  function showStudioResult(f, kind, prompt, payload, index, total) {
    var src = studioFileUrl(f);
    var res = $("#studio-result");
    var media = kind === "video"
      ? '<video class="studio-media" src="' + src + '" controls autoplay loop muted playsinline></video>'
      : '<img class="studio-media" src="' + src + '" alt="result">';
    var created = new Date().toLocaleString();
    var item = {
      kind: kind,
      src: src,
      filename: f.filename,
      created: created,
      prompt: prompt,
      engine: payload.engine,
      quality: payload.quality,
      seconds: payload.seconds,
      width: payload.width,
      height: payload.height,
      sourceWidth: payload.sourceWidth,
      sourceHeight: payload.sourceHeight,
      steps: payload.steps,
      cfg: payload.cfg,
      seed: payload.seed,
      checkpoint: payload.checkpoint,
      sampler: payload.sampler,
      scheduler: payload.scheduler
    };
    studioAddHistory(item);
    res.innerHTML = media + '<div class="studio-result-bar"><span>' +
      esc(total > 1 ? ("Pull " + index + " / " + total) : "Created") +
      '</span><span>' + esc(studioResultMeta(item)) + '</span><a class="mlink" href="' + src + '" download>Download &#8599;</a></div>';
  }

  var CODE_MODE_INFO = {
    hybrid: {
      label: "Hybrid",
      placeholder: 'e.g. Change the app name in the UI from "Luma" to "Luna"'
    },
    local: {
      label: "Local",
      placeholder: "Describe the local code change to make in the selected target..."
    },
    local_web: {
      label: "Local + Web",
      placeholder: "Describe the change and any library, API, or docs context to verify..."
    }
  };
  var codeRun = {
    streaming: false,        // opencode ndjson stream in flight
    busy: false,             // mediaops call in flight
    controller: null,
    stopRequested: false,
    stoppedTarget: null,
    enabled: true,
    runner: "manual_task_file",
    status: null,
    projects: [],
    media: null,             // /api/mediaops status payload
    targetTouched: false,
    loaded: false,
    mode: (function () {
      try { return localStorage.getItem("rigCodeMode") || "hybrid"; }
      catch (e) { return "hybrid"; }
    })()
  };

  function normalizeCodeMode(mode) { return CODE_MODE_INFO[mode] ? mode : "hybrid"; }

  function codeTarget() {
    var sel = $("#code-target");
    var v = (sel && sel.value) || "";
    if (v.slice(0, 6) === "media:") return { kind: "media", key: v.slice(6) };
    if (v.slice(0, 5) === "proj:") return { kind: "proj", key: v.slice(5) };
    return { kind: "", key: "" };
  }

  function renderOpenCodeStatus(status) {
    var box = document.getElementById("opencode-status");
    if (!box) return;
    if (!status) { box.textContent = "checking runner status..."; return; }
    var wsl = status.wsl || {};
    var recommended = status.recommended_runner || "manual_task_file";
    var bridge = "";
    if (codeRun.media) {
      bridge = (codeRun.media.ssh_configured || codeRun.media.workspace_exists)
        ? " · media bridge " + (codeRun.media.ssh_configured ? "ready" : "needs SSH key/password")
        : "";
    }
    box.textContent = "runner: " + recommended +
      (wsl.ready ? " (WSL ready)" : " (WSL not ready — task-file fallback)") + bridge;
    if (!wsl.ready) {
      box.textContent = "runner: " + recommended + " (WSL required - native Windows disabled)" + bridge;
    }
  }

  function populateCodeTargets() {
    var sel = $("#code-target"); if (!sel) return;
    var cur = sel.value;
    var h = "";
    var media = (codeRun.media && codeRun.media.targets) || {};
    var mKeys = Object.keys(media);
    if (mKeys.length) {
      h += '<optgroup label="Media server (validated deploys)">';
      for (var i = 0; i < mKeys.length; i++) {
        h += '<option value="media:' + esc(mKeys[i]) + '">' + esc(media[mKeys[i]].label || mKeys[i]) + '</option>';
      }
      h += '</optgroup>';
    }
    if (codeRun.projects.length) {
      h += '<optgroup label="This rig (opencode)">';
      for (var p = 0; p < codeRun.projects.length; p++) {
        var path = codeRun.projects[p];
        h += '<option value="proj:' + esc(path) + '">' + esc(path.replace(/^.*[\\\/]/, "")) + '</option>';
      }
      h += '</optgroup>';
    }
    sel.innerHTML = h || '<option value="">no targets found</option>';
    if (cur) {
      sel.value = cur;
    } else {
      // The browser auto-selects the first option, so check for the preferred
      // default explicitly: Jellyfin UI is the most common job.
      for (var d = 0; d < sel.options.length; d++) {
        if (sel.options[d].value === "media:jellyfin-ui") { sel.value = "media:jellyfin-ui"; break; }
      }
    }
    if (!sel.value && sel.options.length) sel.selectedIndex = 0;
    applyCodeTarget();
  }

  function applyCodeTarget() {
    var t = codeTarget();
    var isMedia = t.kind === "media";
    var media = (codeRun.media && codeRun.media.targets) || {};
    var depWrap = $("#code-deploy-wrap");
    if (depWrap) depWrap.style.display = isMedia ? "" : "none";
    var sudoWrap = $("#code-sudo-wrap");
    if (sudoWrap) sudoWrap.style.display = isMedia ? "" : "none";
    var note = $("#code-safety-note");
    if (note) {
      if (isMedia) {
        note.textContent = "Deploys validate first and preserve edits on failure. Fill Media password if the key is unavailable.";
      } else if (codeRun.runner === "manual_task_file") {
        note.textContent = "Safe mode: writes a reviewable task file; no agent is launched.";
      } else {
        note.textContent = "Live agent (" + codeRun.runner + ") edits files in the selected project.";
      }
    }
    var btn = $("#code-run");
    if (btn && !codeRun.streaming && !codeRun.busy) {
      var dep = $("#code-deploy");
      btn.textContent = isMedia
        ? ((dep && dep.checked) ? "Run AI Edit + Deploy" : "Run AI Edit")
        : (codeRun.runner === "manual_task_file" ? "Create Task" : "Run AI Edit");
    }
  }

  function applyCodeMode() {
    codeRun.mode = normalizeCodeMode(codeRun.mode);
    var info = CODE_MODE_INFO[codeRun.mode];
    var task = $("#code-task");
    if (task) task.placeholder = info.placeholder;
    var buttons = $$("#code-mode .seg-btn[data-code-mode]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("active", buttons[i].getAttribute("data-code-mode") === codeRun.mode);
      buttons[i].disabled = codeRun.streaming || codeRun.busy;
    }
    applyCodeTarget();
  }

  function setCodeMode(mode) {
    codeRun.mode = normalizeCodeMode(mode);
    try { localStorage.setItem("rigCodeMode", codeRun.mode); } catch (e) {}
    applyCodeMode();
  }

  function fetchCodeInfo() {
    codeRun.loaded = true;
    fetch("/api/opencode/projects", { cache: "no-store" })
      .then(parseJsonResponse)
      .then(function (j) {
        if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "load failed"));
        codeRun.enabled = true;
        codeRun.status = j.runners || null;
        codeRun.runner = (codeRun.status && codeRun.status.recommended_runner) || "manual_task_file";
        codeRun.projects = j.projects || [];
        renderOpenCodeStatus(codeRun.status);
        populateCodeTargets();
        applyCodeMode();
      })
      .catch(function (e) {
        codeRun.enabled = false;
        var box = $("#opencode-status");
        if (box) box.textContent = "runner status failed: " + ((e && e.message) || "network error");
      });
    fetch("/api/mediaops", { cache: "no-store" })
      .then(parseJsonResponse)
      .then(function (j) {
        codeRun.media = (j && j._httpOk && j.ok !== false) ? j : null;
        renderOpenCodeStatus(codeRun.status);
        populateCodeTargets();
      })
      .catch(function () {});
  }

  function renderCodeInit() {
    if (!codeRun.loaded) fetchCodeInfo(); else applyCodeMode();
  }

  var codeTickEl = null;

  function codeLineClass(base, line) {
    var s = (line || "").trim();
    if (/^(PASS|OK)\b|^= finished \(exit 0\)/.test(s)) return base + " ok";
    if (/^(WARN|warning)\b/i.test(s)) return base + " warn";
    if (/^(ERROR|FAILED|FAIL|✗)\b|failed|aborted/i.test(s)) return base + " err";
    if (/^== .+ ==$/.test(s)) return base + " phase";
    if (/^[$>]/.test(s)) return base + " cmd";
    if (/^(→|←|✱|# Todos|## )/.test(s)) return base + " ai";
    return base;
  }

  // Parse one opencode stream line into a tool step {tool, target}, or null.
  // opencode's `run` prints a tool call as "→ Read path [args]", "✱ Grep
  // \"pat\" N matches", or "$ <shell command>" (Bash). Content lines, markdown
  // headings and the final answer never start with those action glyphs, so
  // they fall through to null and render as plain log lines.
  var CODE_TOOL_GLYPHS = "→→✱⚙▸▶•❯";
  function parseToolCall(line) {
    var s = String(line == null ? "" : line)
      .replace(/\[[0-9;?]*[ -\/]*[@-~]/g, "").trim();
    if (!s) return null;
    var m = s.match(/^\$\s+(\S.*)$/);
    if (m) return { tool: "Bash", target: m[1].trim() };
    m = s.match(new RegExp("^[" + CODE_TOOL_GLYPHS + "]+\\s*([A-Z][A-Za-z_-]{1,20})\\s*(.*)$"));
    if (!m) return null;
    var tool = m[1];
    var rest = (m[2] || "").trim().replace(/\s*\[[^\]]*\]\s*$/, "");     // "[offset=1, limit=20]"
    var quoted = rest.match(/^(["'])([\s\S]*?)\1/);                      // Grep/Glob: 'pat'
    if (quoted) return { tool: tool, target: quoted[2] };
    rest = rest
      .replace(/\s+in\s+\S.*$/i, "")                                    // "in ." / "in app/static"
      .replace(/\s*[·•]?\s*\d+\s+(match(?:es)?|lines?|files?|results?|items?)\s*$/i, "")  // "8 matches"
      .trim();
    return { tool: tool, target: rest };
  }

  var _lastCodeStepKey = "";
  function resetCodeSteps() {
    var el = $("#code-steps");
    if (el) { el.innerHTML = ""; el.hidden = true; }
    _lastCodeStepKey = "";
  }
  function pushCodeStep(step) {
    var el = $("#code-steps");
    if (!el || !step || !step.tool) return;
    var key = step.tool + "\u0000" + step.target;
    if (key === _lastCodeStepKey) return;   // opencode repeats the header line
    _lastCodeStepKey = key;
    el.hidden = false;
    var row = document.createElement("div");
    row.className = "code-step";
    var badge = document.createElement("span");
    badge.className = "cs-tool cs-" + step.tool.toLowerCase().replace(/[^a-z]/g, "");
    badge.textContent = step.tool;
    var tgt = document.createElement("span");
    tgt.className = "cs-target";
    tgt.textContent = step.target || "";
    row.appendChild(badge);
    row.appendChild(tgt);
    el.appendChild(row);
    el.scrollTop = el.scrollHeight;
  }

  function appendCodeLine(cls, text) {
    var out = $("#code-output");
    if (!out) return;
    var lines = String(text || "").split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      if (lines.length > 1 && !lines[i].trim()) continue;
      var div = document.createElement("div");
      div.className = "cl " + codeLineClass(cls, lines[i]);
      div.textContent = lines[i];
      out.appendChild(div);
    }
    // keep the heartbeat line pinned to the bottom
    if (codeTickEl && codeTickEl.parentNode === out) out.appendChild(codeTickEl);
    out.scrollTop = out.scrollHeight;
  }

  // Heartbeat: one line that updates in place while the agent is silent
  // (model load / long inference can be minutes with zero output).
  function updateCodeTick(seconds, label) {
    var out = $("#code-output");
    if (!out) return;
    if (!codeTickEl) {
      codeTickEl = document.createElement("div");
      codeTickEl.className = "cl tick";
      out.appendChild(codeTickEl);
    } else if (codeTickEl.parentNode !== out) {
      out.appendChild(codeTickEl);
    }
    var mins = Math.floor(seconds / 60), secs = seconds % 60;
    var t = mins ? (mins + "m " + secs + "s") : (secs + "s");
    codeTickEl.textContent = "⏳ still working · " + t +
      (label ? " · " + label : "") +
      (seconds < 120 ? "  (model may be loading)" : "");
    out.scrollTop = out.scrollHeight;
  }

  function clearCodeTick() {
    if (codeTickEl && codeTickEl.parentNode) codeTickEl.parentNode.removeChild(codeTickEl);
    codeTickEl = null;
  }

  // Shared ndjson consumer for both Code streams (opencode + media safe-fix).
  function consumeCodeEvent(o) {
    if (o.type === "out") {
      var step = parseToolCall(o.text);
      if (step) pushCodeStep(step);
      appendCodeLine("out", o.text);
    }
    else if (o.type === "research") appendCodeLine("research", o.text);
    else if (o.type === "verify") appendCodeLine("research", o.text);
    else if (o.type === "phase") appendCodeLine("meta", "== " + o.text + " ==");
    else if (o.type === "tick") { updateCodeTick(o.seconds || 0, o.text || ""); return; }
    else if (o.type === "error") appendCodeLine("err", o.text);
    else if (o.type === "diff") appendCodeLine("diff", o.text);
    else if (o.type === "done") appendCodeLine("meta", "= finished (exit " + o.code + ")");
    clearCodeTick();
  }

  function setCodeRunning(b) {
    var btn = $("#code-run");
    if (btn) {
      if (b) { btn.textContent = "Stop"; btn.classList.add("stop"); btn.disabled = false; }
      else { btn.classList.remove("stop"); btn.disabled = !codeRun.enabled; }
    }
    var sel = $("#code-target");
    if (sel) sel.disabled = b;
    if (!b) applyCodeMode();
  }

  function setCodeBusy(b) {
    codeRun.busy = b;
    var btn = $("#code-run");
    if (btn) { btn.disabled = b; if (b) btn.textContent = "Working..."; }
    $$(".mediaops-action").forEach(function (x) { x.disabled = b; });
    if (!b) applyCodeMode();
  }

  function endCode() {
    codeRun.streaming = false;
    codeRun.controller = null;
    setCodeRunning(false);
  }
  function stopCode() {
    if (codeRun.controller) {
      codeRun.stopRequested = true;
      codeRun.stoppedTarget = codeTarget();
      try { codeRun.controller.abort(); } catch (e) {}
    }
  }

  function looksLikeJellyfinTask(text) {
    return /jell[yi]fin|home view|view all/i.test(text || "");
  }

  // If the task text is clearly about the Jellyfin UI and the user hasn't
  // touched the target select, flip it to the Jellyfin media target.
  function maybeAutoCodeTarget() {
    if (codeRun.targetTouched) return;
    var sel = $("#code-target"), taskEl = $("#code-task");
    if (!sel || !taskEl || !looksLikeJellyfinTask(taskEl.value)) return;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === "media:jellyfin-ui") {
        sel.value = "media:jellyfin-ui";
        applyCodeTarget();
        break;
      }
    }
  }

  // Guarded media pipeline call; result lands in the shared Code output.
  function runCodeOp(action, payload) {
    if (codeRun.busy || codeRun.streaming) return;
    var out = $("#code-output"); if (!out) return;
    setCodeBusy(true);
    out.textContent = "running " + action + "...";
    fetch("/api/mediaops/" + action, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(parseJsonResponse).then(function (j) {
      var ok = !!(j._httpOk && j.ok);
      var code = (j.code !== undefined && j.code !== null) ? " (exit " + j.code + ")" : "";
      out.textContent = (ok ? "OK" : "FAILED") + code + "\n\n" + (j.output || j.error || ("HTTP " + (j._status || "error")));
      out.scrollTop = 0;
    }).catch(function (e) {
      out.textContent = "FAILED\n\n" + ((e && e.message) || "network error");
    }).then(function () { setCodeBusy(false); });
  }

  function codeMediaPayload(t) {
    return {
      target: t.key || "jellyfin-ui",
      task: ($("#code-task") && $("#code-task").value || "").trim(),
      message: ($("#code-commit") && $("#code-commit").value || "").trim(),
      sudo_password: ($("#code-sudo") && $("#code-sudo").value) || "",
      deploy: !!($("#code-deploy") && $("#code-deploy").checked)
    };
  }

  function cleanupStoppedCodeRun() {
    var t = codeRun.stoppedTarget;
    codeRun.stoppedTarget = null;
    if (!t || t.kind !== "media") return Promise.resolve();
    if (t.key !== "jellyfin-ui") {
      appendCodeLine("err", "= stop cleanup skipped: no reset action for " + t.key);
      return Promise.resolve();
    }
    var outputText = ($("#code-output") && $("#code-output").textContent) || "";
    var reachedDeploy = /== Deploy Jellyfin UI ==|== Deploy jellyfin-ui ==|live-jellyfin-backups\/jellyfin-live-/.test(outputText);
    var payload = {
      target: t.key,
      sudo_password: ($("#code-sudo") && $("#code-sudo").value) || ""
    };
    function stopCleanupAction(action, label) {
      appendCodeLine("meta", "= stop cleanup: " + label);
      return fetch("/api/mediaops/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(parseJsonResponse).then(function (j) {
        var ok = !!(j._httpOk && j.ok);
        appendCodeLine(ok ? "ok" : "err",
          (ok ? "= " + label + " complete" : "= " + label + " failed") +
          (j.output ? "\n" + j.output : (j.error ? "\n" + j.error : "")));
      });
    }
    var chain = reachedDeploy
      ? stopCleanupAction("jellyfin-restore-latest", "restoring latest Jellyfin live backup")
      : Promise.resolve();
    return chain.then(function () {
      return stopCleanupAction("jellyfin-reset-source", "reverting Jellyfin source files");
    }).then(function () {
      appendCodeLine("ok", "= stop cleanup complete");
    }).catch(function (e) {
      appendCodeLine("err", "= stop cleanup failed: " + ((e && e.message) || "network error"));
    });
  }

  function runCode() {
    if (codeRun.streaming) { stopCode(); return; }
    if (codeRun.busy) return;
    var out = $("#code-output"); if (!out) return;
    maybeAutoCodeTarget();
    var t = codeTarget();
    var taskEl = $("#code-task");
    var task = (taskEl && taskEl.value || "").trim();
    if (!t.key) { out.textContent = "Pick a target first."; return; }
    if (!task) { if (taskEl) taskEl.focus(); return; }

    var url, payload;
    if (t.kind === "media") {
      url = "/api/mediaops/safe-fix/stream";
      payload = codeMediaPayload(t);
    } else {
      url = "/api/opencode/run";
      payload = { project: t.key, task: task, mode: codeRun.mode, runner: codeRun.runner,
                  confirm_run: codeRun.runner !== "manual_task_file" };
    }

    var info = CODE_MODE_INFO[normalizeCodeMode(codeRun.mode)];
    out.innerHTML = "";
    resetCodeSteps();
    clearCodeTick();
    appendCodeLine("meta", "> " + info.label + " / " +
      (t.kind === "media" ? "media pipeline" : codeRun.runner) + "  -  " + t.key);
    codeRun.streaming = true;
    codeRun.stopRequested = false;
    codeRun.stoppedTarget = null;
    setCodeRunning(true);
    codeRun.controller = new AbortController();
    fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: codeRun.controller.signal
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("run failed (" + resp.status + ")");
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
      function consume(line) {
        var ln = line.trim(); if (!ln) return;
        var o; try { o = JSON.parse(ln); } catch (e) { return; }
        consumeCodeEvent(o);
      }
      function pump() {
        return reader.read().then(function (rd) {
          if (rd.done) { buf += dec.decode(); if (buf.trim()) consume(buf); clearCodeTick(); endCode(); return; }
          buf += dec.decode(rd.value, { stream: true });
          var parts = buf.split("\n"); buf = parts.pop();
          for (var i = 0; i < parts.length; i++) consume(parts[i]);
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      clearCodeTick();
      if (e && e.name === "AbortError") {
        appendCodeLine("meta", "= stopped");
        return cleanupStoppedCodeRun().then(function () { endCode(); });
      }
      appendCodeLine("err", (e && e.message) || "request failed");
      endCode();
    });
  }

  // ---------- main render dispatch ----------
  function renderAll() {
    if (!data) return;
    try { renderOverview(data); } catch (e) {}
    try { renderEco(); } catch (e) {}
    try { renderGpu(data); } catch (e) {}
    try { renderCpu(data); } catch (e) {}
    try { renderMem(data); } catch (e) {}
    try { renderDisks(data); } catch (e) {}
    try { renderNet(data); } catch (e) {}
    try { renderProc(data); } catch (e) {}
    try { renderWarn(data); } catch (e) {}
    try { renderRaw(data); } catch (e) {}

    try { renderHeaderVitals(data); } catch (e) {}
    var upd = $("#last-updated");
    if (upd) upd.textContent = new Date().toLocaleTimeString();
    var pill = $("#status-pill");
    if (pill) {
      if (!online) { pill.textContent = "Offline"; pill.className = "pill dead"; }
      else if (data.warnings && data.warnings.length) { pill.textContent = "Warnings"; pill.className = "pill warn"; }
      else { pill.textContent = "Live"; pill.className = "pill live"; }
    }
    var hostEl = $("#host");
    if (hostEl && data.hostname) hostEl.textContent = data.hostname + "  ·  " + safe(data.rig_name);
  }

  function collectHistory(d) {
    var cpu = d.cpu || {}, gpu = d.gpu || {}, mem = d.memory || {};
    var net0 = (d.network && d.network[0]) || {};
    push("cpu", d.cpu_percent);
    push("gpu", gpu.gpu_utilization_percent);
    push("vram", gpu.memory_utilization_percent);
    push("ram", mem.percent);
    push("cput", cpu.package_temperature_c != null ? cpu.package_temperature_c : cpu.max_c);
    push("gput", gpu.temperature_c);
    push("netrx", net0.rx_rate_kbps);
    push("nettx", net0.tx_rate_kbps);
  }

  function fetchStats() {
    fetch("/api/stats", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("bad"); return r.json(); })
      .then(function (stats) {
        return fetch("/api/status", { cache: "no-store" })
          .then(function (r) { return r.json(); })
          .then(function (status) {
            online = true;
            data = Object.assign({}, stats, status);
            collectHistory(data);
            renderAll();
          });
      })
      .catch(function () {
        online = false;
        if (data) renderAll(); else {
          var p = $("#status-pill");
          if (p) { p.textContent = "Offline"; p.className = "pill dead"; }
        }
      });
  }

  var statsTick = 0;
  function loop() {
    if (!paused) {
      // Pause the 2s monitoring poll while actively chatting so the stream
      // gets the bandwidth and the GPU isn't being re-queried every tick.
      if (!(activeTab === "assistant" && chat.streaming) && !(activeTab === "code" && codeRun.streaming)) fetchStats();
      statsTick++;
      if (statsTick % 5 === 0) { fetchHub(); if (activeTab === "media") fetchMedia(); }
    }
    setTimeout(loop, 2000);
  }

  function vpill(key, val, level) {
    return '<span class="vpill ' + (level || "") + '"><span class="vk">' + key +
      '</span><span class="vv">' + val + '</span></span>';
  }
  function renderHeaderVitals(d) {
    var el = $("#hdr-vitals");
    if (!el) return;
    if (!d || !online) {
      el.innerHTML = "";
      updateChromeHeight();
      return;
    }
    var gpu = d.gpu || {}, mem = d.memory || {}, cpu = d.cpu || {};
    var cpuP = d.cpu_percent, gpuP = gpu.gpu_utilization_percent, vram = gpu.memory_utilization_percent;
    var cput = cpu.package_temperature_c != null ? cpu.package_temperature_c : cpu.max_c;
    var gput = gpu.temperature_c;
    var h = "";
    h += vpill("CPU", isFiniteNumber(cpuP) ? Math.round(cpuP) + "%" : "-", lvl(cpuP || 0, 70, 90));
    h += vpill("GPU", isFiniteNumber(gpuP) ? Math.round(gpuP) + "%" : "-", lvl(gpuP || 0, 70, 92));
    h += vpill("VRAM", isFiniteNumber(vram) ? Math.round(vram) + "%" : "-", lvl(vram || 0, 80, 94));
    h += vpill("CPU°", isFiniteNumber(cput) ? Math.round(cput) + "°" : "-", lvl(cput || 0, 75, 90));
    h += vpill("GPU°", isFiniteNumber(gput) ? Math.round(gput) + "°" : "-", lvl(gput || 0, 75, 84));
    el.innerHTML = h;
    updateChromeHeight();
  }

  var cmdk = { open: false, items: [], sel: 0 };

  function goTab(id) {
    var btn = null, nbs = $$(".nb");
    for (var i = 0; i < nbs.length; i++) if (nbs[i].dataset && nbs[i].dataset.t === id) { btn = nbs[i]; break; }
    if (btn) btn.onclick();
  }
  function focusLater(sel) {
    setTimeout(function () { var e = $(sel); if (e && e.focus) e.focus(); }, 80);
  }

  function buildCmdkActions() {
    var acts = [];
    // Derived from the DOM so each shell only offers the tabs it actually has.
    var tabs = [];
    $$(".nb").forEach(function (b) {
      if (b.dataset && b.dataset.t) tabs.push([b.dataset.t, (b.textContent || b.dataset.t).trim()]);
    });
    tabs.forEach(function (t) {
      acts.push({ cat: "Go", icon: "&#8594;", title: t[1], sub: "Open the " + t[1] + " tab",
        run: function () { goTab(t[0]); } });
    });
    function hasTab(id) {
      for (var i = 0; i < tabs.length; i++) if (tabs[i][0] === id) return true;
      return false;
    }
    var sysPanes = [
      ["gpu", "GPU"], ["cpu", "CPU"], ["mem", "Memory"], ["disks", "Disks"],
      ["net", "Network"], ["proc", "Processes"], ["rgb", "Lighting"], ["raw", "Raw"]
    ];
    if (hasTab("system")) sysPanes.forEach(function (t) {
      acts.push({ cat: "Go", icon: "&#8594;", title: t[1], sub: "System · " + t[1],
        run: function () { goTab("system"); goSys(t[0]); } });
    });
    // Models (load / unload)
    var models = ecoState.models || [], loaded = {};
    (ecoState.modelLoaded || []).forEach(function (m) { loaded[m.name] = true; });
    models.forEach(function (m) {
      var isUp = !!loaded[m.name];
      acts.push({ cat: "Model", icon: isUp ? "&#9211;" : "&#9654;",
        title: (isUp ? "Unload " : "Load ") + m.name,
        sub: (m.params ? m.params + " · " : "") + (m.size_gb != null ? m.size_gb + " GB" : "") + (isUp ? " · in VRAM" : ""),
        run: function () { hubAction(m.name, isUp ? "unload" : "load"); } });
    });
    // Services (start / stop)
    (ecoState.services || []).forEach(function (s) {
      if (!s.control) return;
      var act = s.up ? "stop" : "start";
      acts.push({ cat: "Service", icon: s.up ? "&#9209;" : "&#9654;",
        title: (s.up ? "Stop " : "Start ") + s.name, sub: s.host + ":" + s.port,
        run: function () { serviceAction(s.id, act); if (activeTab !== "aihub") goTab("aihub"); } });
    });
    // Quick actions
    if (hasTab("assistant")) {
      acts.push({ cat: "Chat", icon: "&#129504;", title: "Toggle assistant memory",
        sub: assistantMem ? "currently ON" : "currently OFF", run: function () { setAssistantMem(!assistantMem); } });
      acts.push({ cat: "Chat", icon: "+", title: "New chat", sub: "Clear the assistant conversation",
        run: function () { goTab("assistant"); var b = $("#chat-new"); if (b) b.onclick(); } });
    }
    if (hasTab("goals")) {
      acts.push({ cat: "Goals", icon: "&#9678;", title: "Open Goals", sub: "Big goals, small goals, momentum",
        run: function () { goTab("goals"); } });
      acts.push({ cat: "Goals", icon: "&#43;", title: "Add a goal", sub: "New big or small goal",
        run: function () { goTab("goals"); focusLater("#gl-new-title"); } });
    }
    if (hasTab("hermes")) {
      acts.push({ cat: "Hermes", icon: "&#43;", title: "Add a memory", sub: "Save a preference, fact, or rule",
        run: function () { goTab("hermes"); focusLater("#hermes-content"); } });
      acts.push({ cat: "Hermes", icon: "&#128270;", title: "Run an AI review", sub: "Hermes-aware local or cloud review",
        run: function () { goTab("hermes"); focusLater("#hermes-rev-prompt"); } });
    }
    if ($("#btn-pause")) {
      acts.push({ cat: "System", icon: paused ? "&#9654;" : "&#10073;&#10073;", title: paused ? "Resume monitoring" : "Pause monitoring",
        sub: "Toggle the 2s polling loop", run: function () { var b = $("#btn-pause"); if (b) b.onclick(); } });
    }
    acts.push({ cat: "System", icon: "&#8635;", title: "Refresh now", sub: "Re-fetch stats and services",
      run: function () { var b = $("#btn-refresh"); if (b) b.onclick(); } });
    if (hasTab("system")) {
      acts.push({ cat: "System", icon: "&#9112;", title: "Copy latest JSON", sub: "Copy the raw stats payload",
        run: function () { goTab("system"); goSys("raw"); var b = $("#btn-copy"); if (b) b.onclick(); } });
      // Lighting
      RGB_MODES.forEach(function (m) {
        acts.push({ cat: "Lighting", icon: "&#9673;", title: "Lighting: " + m[1],
          sub: RGB_MODE_HELP[m[0]] || "",
          run: function () { goTab("system"); goSys("rgb"); rgbPost("config", { mode: m[0], enabled: m[0] !== "off" }); } });
      });
      acts.push({ cat: "Lighting", icon: "&#9678;", title: "Lighting: test pattern",
        sub: "Red / green / blue / white sweep across the case",
        run: function () { goTab("system"); goSys("rgb"); rgbPost("test", {}); } });
    }
    if (hasTab("terminal")) {
      acts.push({ cat: "System", icon: "&#62;_", title: "Open Terminal", sub: "Run a command on the rig",
        run: function () { goTab("terminal"); focusLater("#term-cmd"); } });
      acts.push({ cat: "System", icon: "&#8635;", title: "Restart dashboard service", sub: "No SSH — restarts SysMonitor",
        run: function () { goTab("terminal"); restartService(); } });
    }
    if (hasTab("messaging")) {
      acts.push({ cat: "Messaging", icon: "&#9993;", title: "Open Messaging", sub: "Auto-reply, digest, inbox",
        run: function () { goTab("messaging"); } });
    }
    // Jump to the other shell.
    var otherApp = (document.body && document.body.getAttribute("data-app")) === "personal"
      ? { href: "/", title: "Open the Console", sub: "Rig monitoring, terminal, services" }
      : { href: "/me", title: "Open Personal", sub: "Assistant, goals, memory" };
    acts.push({ cat: "Go", icon: "&#8599;", title: otherApp.title, sub: otherApp.sub,
      run: function () { window.location.href = otherApp.href; } });
    return acts;
  }

  function cmdkFilter(q) {
    q = (q || "").trim().toLowerCase();
    var all = buildCmdkActions();
    if (!q) return all.slice(0, 60);
    var terms = q.split(/\s+/);
    return all.filter(function (a) {
      var hay = (a.title + " " + a.sub + " " + a.cat).toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) >= 0; });
    }).slice(0, 60);
  }

  function renderCmdk() {
    var list = $("#cmdk-list");
    if (!list) return;
    var items = cmdk.items;
    if (!items.length) { list.innerHTML = '<div class="cmdk-empty">No matching actions</div>'; return; }
    if (cmdk.sel >= items.length) cmdk.sel = items.length - 1;
    if (cmdk.sel < 0) cmdk.sel = 0;
    var h = "";
    for (var i = 0; i < items.length; i++) {
      var a = items[i];
      h += '<div class="cmdk-item' + (i === cmdk.sel ? " sel" : "") + '" data-i="' + i + '">' +
        '<span class="cmdk-ic">' + a.icon + '</span>' +
        '<span class="cmdk-tx"><span class="cmdk-ti">' + esc(a.title) + '</span>' +
        '<span class="cmdk-sub">' + esc(a.sub || "") + '</span></span>' +
        '<span class="cmdk-cat">' + esc(a.cat) + '</span>' +
      '</div>';
    }
    list.innerHTML = h;
  }

  function openCmdk() {
    var box = $("#cmdk"); if (!box) return;
    cmdk.open = true; cmdk.sel = 0;
    cmdk.items = cmdkFilter("");
    box.hidden = false;
    renderCmdk();
    var inp = $("#cmdk-input");
    if (inp) { inp.value = ""; setTimeout(function () { inp.focus(); }, 30); }
  }
  function closeCmdk() {
    var box = $("#cmdk"); if (!box) return;
    cmdk.open = false; box.hidden = true;
  }
  function runCmdkSel() {
    var a = cmdk.items[cmdk.sel];
    closeCmdk();
    if (a && a.run) { try { a.run(); } catch (e) {} }
  }

  function setAssistantMem(on) {
    assistantMem = !!on;
    try { localStorage.setItem("rigconsole.mem", assistantMem ? "1" : "0"); } catch (e) {}
    var b = $("#chat-mem");
    if (b) { b.classList.toggle("on", assistantMem); }
  }

  function setAutoSpeak(on) {
    autoSpeak = !!on;
    try { localStorage.setItem("rigconsole.voice", autoSpeak ? "1" : "0"); } catch (e) {}
    var b = $("#chat-voice");
    if (b) { b.classList.toggle("on", autoSpeak); }
    if (!autoSpeak && chatAudio) { try { chatAudio.pause(); } catch (e) {} }
  }

  function speakText(text, btn) {
    var clean = String(text || "").trim();
    if (!clean) return;
    if (chatAudio) { try { chatAudio.pause(); } catch (e) {} chatAudio = null; }
    if (btn) { btn.dataset.label = btn.innerHTML; btn.innerHTML = "&#8987; ..."; btn.disabled = true; }
    fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: clean })
    }).then(function (resp) {
      if (resp.status === 503) throw new Error("warming");
      if (!resp.ok) throw new Error("tts " + resp.status);
      return resp.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      chatAudio = new Audio(url);
      chatAudio.onended = chatAudio.onerror = function () { try { URL.revokeObjectURL(url); } catch (e) {} };
      chatAudio.play().catch(function () {});
      if (btn) { btn.innerHTML = btn.dataset.label || "&#128266; Speak"; btn.disabled = false; }
    }).catch(function (e) {
      if (btn) {
        btn.innerHTML = /warming/.test(e.message) ? "Voice warming up..." : "Voice failed";
        btn.disabled = false;
        setTimeout(function () { btn.innerHTML = btn.dataset.label || "&#128266; Speak"; }, 2500);
      }
    });
  }

  var term = { cfg: null, streaming: false, controller: null, tailId: null, tailTimer: null, tailOffset: 0 };
  var TERM_QUICK = ["nvidia-smi", "ollama ps", "ollama list", "python --version", "docker ps", "Get-Date"];

  function fetchExecConfig(cb) {
    fetch("/api/exec/config", { cache: "no-store" }).then(parseJsonResponse).then(function (j) {
      if (j && j._httpOk) { term.cfg = j; populateTermCwd(); }
      if (cb) cb();
    }).catch(function () {});
  }
  function populateTermCwd() {
    var sel = $("#term-cwd"); if (!sel || !term.cfg) return;
    var cur = sel.value;
    sel.innerHTML = (term.cfg.cwds || []).map(function (p) {
      return '<option value="' + esc(p) + '">' + esc(p) + '</option>';
    }).join("");
    if (cur) sel.value = cur; else if (term.cfg.default_cwd) sel.value = term.cfg.default_cwd;
  }
  function renderTermQuick() {
    var el = $("#term-quick"); if (!el) return;
    el.innerHTML = TERM_QUICK.map(function (c) {
      return '<button class="term-chip" type="button" data-cmd="' + esc(c) + '">' + esc(c) + '</button>';
    }).join("");
  }
  function renderTerminalInit() {
    if (!term.cfg) fetchExecConfig(); else populateTermCwd();
    renderTermQuick();
    refreshProcs();
  }
  function termAppend(text, cls) {
    var out = $("#term-output"); if (!out) return;
    var span = document.createElement("span");
    if (cls) span.className = "t-" + cls;
    span.textContent = text + "\n";
    out.appendChild(span);
    out.scrollTop = out.scrollHeight;
  }
  function setTermRunning(b) {
    var run = $("#term-run"); if (run) { run.textContent = b ? "Stop" : "Run"; run.classList.toggle("stop", b); }
  }
  function termCmdVal() { var i = $("#term-cmd"); return i ? (i.value || "").trim() : ""; }
  function termPayload(extra) {
    var p = {
      command: termCmdVal(),
      cwd: ($("#term-cwd") && $("#term-cwd").value) || "",
      shell: ($("#term-shell") && $("#term-shell").value) || "powershell",
      confirm: true
    };
    if (extra) for (var k in extra) p[k] = extra[k];
    return p;
  }
  function runTerm() {
    if (term.streaming) { stopTermStream(); return; }
    var cmd = termCmdVal(); if (!cmd) return;
    var out = $("#term-output"); if (out) out.innerHTML = "";
    termAppend("$ " + cmd, "cmd");
    term.streaming = true; setTermRunning(true);
    term.controller = new AbortController();
    fetch("/api/exec/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(termPayload()), signal: term.controller.signal
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("exec failed (" + resp.status + ")");
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
      function consume(line) {
        var ln = line.trim(); if (!ln) return;
        var o; try { o = JSON.parse(ln); } catch (e) { return; }
        if (o.type === "out") termAppend(o.text || "");
        else if (o.type === "start") termAppend("[pid " + o.pid + " · " + o.shell + " · " + o.cwd + "]", "meta");
        else if (o.type === "error") termAppend(o.text || "error", "err");
        else if (o.type === "done") termAppend("= exit " + o.code, "meta");
      }
      function pump() {
        return reader.read().then(function (rd) {
          if (rd.done) { buf += dec.decode(); if (buf.trim()) consume(buf); endTermStream(); return; }
          buf += dec.decode(rd.value, { stream: true });
          var parts = buf.split("\n"); buf = parts.pop();
          for (var i = 0; i < parts.length; i++) consume(parts[i]);
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      if (e && e.name === "AbortError") termAppend("[stopped]", "meta");
      else termAppend("Error: " + (e && e.message ? e.message : "network error"), "err");
      endTermStream();
    });
  }
  function endTermStream() { term.streaming = false; term.controller = null; setTermRunning(false); }
  function stopTermStream() { if (term.controller) { try { term.controller.abort(); } catch (e) {} } }
  function startTermBg() {
    var cmd = termCmdVal(); if (!cmd) return;
    termAppend("[background] " + cmd, "meta");
    fetch("/api/exec/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(termPayload({ label: cmd }))
    }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) { termAppend("Error: " + responseError(j, "start failed"), "err"); return; }
      termAppend("started pid " + j.pid + " (id " + j.id + ")", "meta");
      refreshProcs(); startTail(j.id);
    }).catch(function () { termAppend("Error: network", "err"); });
  }
  function refreshProcs() {
    fetch("/api/exec/list", { cache: "no-store" }).then(parseJsonResponse).then(function (j) {
      term.procs = (j && j.processes) || []; renderProcs();
    }).catch(function () {});
  }
  function renderProcs() {
    var el = $("#term-procs"); if (!el) return;
    var procs = term.procs || [];
    if (!procs.length) { el.innerHTML = '<div class="muted">No background processes.</div>'; return; }
    el.innerHTML = procs.map(function (p) {
      return '<div class="term-proc' + (p.running ? ' on' : '') + '">' +
        '<div class="tp-main"><span class="tp-dot ' + (p.running ? 'on' : 'off') + '"></span>' +
        '<span class="tp-cmd">' + esc(p.label || p.command) + '</span>' +
        '<span class="tp-meta">pid ' + p.pid + ' · ' + (p.running ? Math.round(p.uptime_s) + 's' : 'stopped') + '</span></div>' +
        '<div class="tp-actions">' +
          '<button class="tp-btn" type="button" data-tail="' + p.id + '">Tail</button>' +
          (p.running ? '<button class="tp-btn stop" type="button" data-stop="' + p.id + '">Stop</button>' : '') +
        '</div></div>';
    }).join("");
  }
  function startTail(id) {
    term.tailId = id; term.tailOffset = 0;
    if (term.tailTimer) clearInterval(term.tailTimer);
    termAppend("[tailing " + id + "]", "meta");
    pollTail();
    term.tailTimer = setInterval(pollTail, 1500);
  }
  function pollTail() {
    if (!term.tailId) return;
    fetch("/api/exec/tail?id=" + encodeURIComponent(term.tailId) + "&offset=" + term.tailOffset, { cache: "no-store" })
      .then(parseJsonResponse).then(function (j) {
        if (!j._httpOk || j.ok === false) return;
        if (j.text) termAppend(j.text.replace(/\s+$/, ""), "out");
        term.tailOffset = j.offset != null ? j.offset : term.tailOffset;
        if (!j.running) { stopTail(); termAppend("[process ended]", "meta"); refreshProcs(); }
      }).catch(function () {});
  }
  function stopTail() { if (term.tailTimer) { clearInterval(term.tailTimer); term.tailTimer = null; } term.tailId = null; }
  function stopProc(id) {
    fetch("/api/exec/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: id }) })
      .then(function () { refreshProcs(); }).catch(function () {});
  }
  function restartService() {
    if (!window.confirm("Restart the dashboard service now? The page will reconnect in a few seconds.")) return;
    termAppend("[restarting service...]", "meta");
    fetch("/api/service/restart", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true })
    }).then(parseJsonResponse).then(function (j) {
      termAppend(j && j.ok ? "service restarting; reconnecting..." : "restart failed", "meta");
    }).catch(function () { termAppend("restart sent (connection dropped, expected)", "meta"); });
  }

  // ---------- controls ----------
  // This file backs two shells (/ = console, /me = personal), so every control
  // is optional — never assume a section exists.
  var btnRefresh = $("#btn-refresh");
  if (btnRefresh) btnRefresh.onclick = function () {
    paused = false;
    var bp = $("#btn-pause"); if (bp) bp.innerHTML = "&#x23F8;";
    fetchStats(); fetchHub();
  };
  var btnPause = $("#btn-pause");
  if (btnPause) btnPause.onclick = function () {
    paused = !paused;
    btnPause.innerHTML = paused ? "&#x25B6;" : "&#x23F8;";
  };
  var btnCopy = $("#btn-copy");
  if (btnCopy) btnCopy.onclick = function () {
    if (!data) return;
    navigator.clipboard.writeText(JSON.stringify(data, null, 2)).then(function () {
      btnCopy.textContent = "Copied!";
      setTimeout(function () { btnCopy.textContent = "Copy Latest JSON"; }, 1500);
    });
  };
  $$(".nb").forEach(function (b) {
    b.onclick = function () {
      $$(".nb").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      $$(".sec").forEach(function (s) { s.classList.remove("active"); });
      var sec = document.getElementById(b.dataset.t);
      if (sec) sec.classList.add("active");
      activeTab = b.dataset.t;
      if (activeTab === "aihub") { renderAihub(); fetchHub(); renderMessagingInit(); }
      if (activeTab === "messaging") { renderMessagingInit(); }
      if (activeTab === "goals") { renderGoals(); fetchGoals(); }
      if (activeTab === "hermes") { renderHermes(); fetchHermes(true); }
      if (activeTab === "media") { renderMedia(); fetchMedia(); }
      if (activeTab === "studio") { renderStudioStatus(); fetchHub(); }
      if (activeTab === "code") { renderCodeInit(); }
      if (activeTab === "terminal") { renderTerminalInit(); } else { stopTail(); }
      if (data) renderAll();
      window.scrollTo(0, 0);
    };
  });

  // ---------- Goals ----------
  var goalsState = null, goalsOpen = {}, goalsSug = {}, goalsBusy = false;
  var GOAL_AREA_HUE = { health: 145, mind: 265, career: 210, money: 45, learning: 190,
                        home: 25, people: 330, fun: 100, unsorted: 220 };

  function areaHue(area) {
    if (GOAL_AREA_HUE[area] != null) return GOAL_AREA_HUE[area];
    var h = 0, s = String(area || "");
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
    return h;
  }
  function areaColor(area, light) { return "hsl(" + areaHue(area) + " 65% " + (light || 58) + "%)"; }

  // Donut used for the overall figure and the per-area rings.
  function donut(pct, size, stroke, color, label, sub) {
    var r = (size - stroke) / 2, c = 2 * Math.PI * r, mid = size / 2;
    var val = clampPct(pct);
    return '<div class="gl-donut" style="width:' + size + 'px">' +
      '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '">' +
        '<circle cx="' + mid + '" cy="' + mid + '" r="' + r + '" fill="none" stroke="var(--inset)" stroke-width="' + stroke + '"/>' +
        '<circle cx="' + mid + '" cy="' + mid + '" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="' + stroke +
          '" stroke-linecap="round" stroke-dasharray="' + c + '" stroke-dashoffset="' + (c * (1 - val / 100)) +
          '" transform="rotate(-90 ' + mid + ' ' + mid + ')"/>' +
        '<text x="' + mid + '" y="' + (mid + 1) + '" text-anchor="middle" dominant-baseline="middle" ' +
          'fill="var(--txt)" font-size="' + Math.round(size / 4.2) + '" font-weight="700">' + Math.round(val) + '</text>' +
      '</svg>' +
      (label ? '<div class="gl-dlabel">' + esc(label) + '</div>' : '') +
      (sub ? '<div class="gl-dsub">' + esc(sub) + '</div>' : '') +
    '</div>';
  }

  // GitHub-style momentum grid: did you actually touch anything that day?
  function momentumGrid(grid) {
    if (!grid || !grid.length) return '';
    var cols = [], week = [];
    // pad so the first column starts on a Monday
    var firstDow = (new Date(grid[0].day + "T00:00:00").getDay() + 6) % 7;
    for (var p = 0; p < firstDow; p++) week.push(null);
    for (var i = 0; i < grid.length; i++) {
      week.push(grid[i]);
      if (week.length === 7) { cols.push(week); week = []; }
    }
    if (week.length) cols.push(week);
    var h = '<div class="gl-grid">';
    for (var c = 0; c < cols.length; c++) {
      h += '<div class="gl-gcol">';
      for (var d = 0; d < 7; d++) {
        var cell = cols[c][d];
        if (!cell) { h += '<span class="gl-cell empty"></span>'; continue; }
        var lvl = cell.count === 0 ? 0 : cell.count === 1 ? 1 : cell.count <= 3 ? 2 : 3;
        h += '<span class="gl-cell l' + lvl + '" title="' + esc(cell.day) + ': ' +
             cell.count + ' check-in' + (cell.count === 1 ? '' : 's') + '"></span>';
      }
      h += '</div>';
    }
    return h + '</div>';
  }

  function goalCard(g, children) {
    var pct = clampPct(g.effective_progress);
    var col = areaColor(g.area || "unsorted");
    var open = !!goalsOpen[g.id];
    var h = '<div class="gl-card' + (g.status === "done" ? ' done' : '') + '" data-goal="' + g.id + '"' +
            ' style="--gcol:' + col + '">' +
      '<div class="gl-chead">' +
        '<button class="gl-check' + (g.checked_today ? ' on' : '') + '" data-gcheck="' + g.id +
          '" title="Log that you worked on this today">' + (g.checked_today ? '&#10003;' : '') + '</button>' +
        '<div class="gl-ctitle">' + esc(g.title) +
          (g.area ? '<span class="gl-area">' + esc(g.area) + '</span>' : '') +
          (g.parent_title ? '<span class="gl-parent">&#8598; ' + esc(g.parent_title) + '</span>' : '') +
        '</div>' +
        '<span class="gl-pct">' + pct + '%</span>' +
      '</div>' +
      '<div class="gl-bar"><span style="width:' + pct + '%;background:' + col + '"></span></div>';

    if (g.child_count) {
      h += '<button class="gl-expand" data-gopen="' + g.id + '">' +
           (open ? '&#9662; ' : '&#9656; ') + g.done_children + ' / ' + g.child_count + ' small goals' +
           (g.derived ? ' &middot; progress rolls up' : '') + '</button>';
    }
    if (open && children && children.length) {
      h += '<div class="gl-kids">';
      for (var i = 0; i < children.length; i++) {
        var k = children[i];
        h += '<div class="gl-kid' + (k.status === "done" ? ' done' : '') + '">' +
          '<button class="gl-tick' + (k.status === "done" ? ' on' : '') + '" data-gdone="' + k.id +
            '" data-was="' + k.status + '">' + (k.status === "done" ? '&#10003;' : '') + '</button>' +
          '<span class="gl-ktitle">' + esc(k.title) + '</span>' +
          '<button class="gl-x" data-gdel="' + k.id + '" title="Delete">&times;</button>' +
        '</div>';
      }
      h += '</div>';
    }
    if (open && goalsSug[g.id]) {
      h += '<div class="gl-sug"><div class="gl-sughead">Suggested by the local model &mdash; click to keep</div>';
      goalsSug[g.id].forEach(function (s, i) {
        h += '<button class="gl-sugitem" data-gsug="' + g.id + '" data-i="' + i + '">+ ' + esc(s) + '</button>';
      });
      h += '</div>';
    }
    if (open) {
      h += '<div class="gl-cacts">' +
        '<input class="gl-addkid" data-gadd="' + g.id + '" placeholder="Add a small goal..." />' +
        '<button class="btn-sm btn-ghost" data-gbreak="' + g.id + '">Break down with AI</button>' +
        '<button class="btn-sm btn-ghost" data-gstatus="' + g.id + '" data-to="' +
          (g.status === "done" ? "active" : "done") + '">' +
          (g.status === "done" ? "Reopen" : "Mark done") + '</button>' +
        '<button class="btn-sm btn-ghost" data-gdel="' + g.id + '">Delete</button>' +
      '</div>';
    } else {
      h += '<button class="gl-more" data-gopen="' + g.id + '">Open</button>';
    }
    return h + '</div>';
  }

  function renderGoals() {
    var box = $("#goals-card");
    if (!box) return;
    if (!goalsState) {
      box.innerHTML = '<div class="gcard"><div class="sk-line sk-h"></div><div class="sk-line"></div><div class="sk-line"></div></div>';
      return;
    }
    var st = goalsState.stats || {};
    var all = goalsState.goals || [];
    var kidsOf = {};
    all.forEach(function (g) {
      if (g.parent_id) { (kidsOf[g.parent_id] = kidsOf[g.parent_id] || []).push(g); }
    });

    // ---- summary strip
    var h = '<div class="gcard gl-top">' +
      donut(st.overall_progress, 108, 11, "var(--accent)", "overall", st.active + " active") +
      '<div class="gl-tstats">' +
        '<div class="gl-stat"><b>' + intgr(goalsState.streak) + '</b><span>day streak</span></div>' +
        '<div class="gl-stat"><b>' + intgr(goalsState.checked_today) + '</b><span>touched today</span></div>' +
        '<div class="gl-stat"><b>' + intgr(st.big) + '</b><span>big goals</span></div>' +
        '<div class="gl-stat"><b>' + intgr(st.small) + '</b><span>small goals</span></div>' +
        '<div class="gl-stat"><b>' + intgr(st.done) + '</b><span>done</span></div>' +
      '</div>' +
      '<div class="gl-momentum">' + momentumGrid(goalsState.grid) +
        '<div class="gl-mlabel">last 13 weeks &middot; darker = more check-ins</div></div>' +
    '</div>';

    // ---- per-area rings
    if ((goalsState.areas || []).length) {
      h += '<div class="gcard"><div class="card-h">By area</div><div class="gl-areas">';
      goalsState.areas.forEach(function (a) {
        h += donut(a.progress, 76, 8, areaColor(a.area), a.area, a.done + "/" + a.total);
      });
      h += '</div></div>';
    }

    // ---- horizon timeline (the big-goal view)
    h += '<div class="gcard"><div class="card-h">Horizons &mdash; near to far</div><div class="gl-tl">';
    (goalsState.horizons || []).forEach(function (hz) {
      var inHz = all.filter(function (g) {
        return g.horizon === hz.key && !g.parent_id && g.status !== "dropped";
      });
      h += '<div class="gl-col' + (hz.big ? ' big' : '') + '">' +
        '<div class="gl-chead2">' + esc(hz.label) + '<span>' + inHz.length + '</span></div>';
      if (!inHz.length) {
        h += '<div class="gl-empty">nothing here</div>';
      } else {
        inHz.forEach(function (g) { h += goalCard(g, kidsOf[g.id]); });
      }
      h += '<button class="gl-addhz" data-ghz="' + hz.key + '">+ add</button></div>';
    });
    h += '</div></div>';

    // ---- new goal form
    h += '<div class="gcard"><div class="card-h">New goal</div>' +
      '<div class="gl-form">' +
        '<input id="gl-new-title" placeholder="What do you want to be true?" />' +
        '<select id="gl-new-horizon" class="chat-select">';
    (goalsState.horizons || []).forEach(function (hz) {
      h += '<option value="' + hz.key + '"' + (hz.key === "week" ? " selected" : "") + '>' + esc(hz.label) + '</option>';
    });
    h += '</select><select id="gl-new-area" class="chat-select"><option value="">no area</option>';
    (goalsState.known_areas || []).forEach(function (a) {
      h += '<option value="' + esc(a) + '">' + esc(a) + '</option>';
    });
    h += '</select><button class="btn-sm" id="gl-new-go">Add</button></div>' +
      '<div class="rgb-note">Big goals (quarter and beyond) hold small goals. Ticking the small ones ' +
      'rolls the big one forward on its own &mdash; and the case lighting can follow it (Lighting &rarr; Goals).</div>' +
    '</div>';

    box.innerHTML = h;
    wireGoals();
  }

  function goalsPost(path, body, method) {
    goalsBusy = true;
    return fetch("/api/goals" + (path || ""), {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: method === "DELETE" ? undefined : JSON.stringify(body || {})
    }).then(parseJsonResponse).finally(function () {
      goalsBusy = false;
      fetchGoals();
    });
  }

  function fetchGoals() {
    return fetch("/api/goals").then(parseJsonResponse).then(function (j) {
      if (j && j.goals) { goalsState = j; renderGoals(); }
      return j;
    }).catch(function () {});
  }

  function wireGoals() {
    var box = $("#goals-card");
    if (!box) return;
    box.onclick = function (e) {
      if (!e.target || !e.target.closest) return;
      var t;
      if ((t = e.target.closest("[data-gopen]"))) {
        var gid = t.getAttribute("data-gopen");
        goalsOpen[gid] = !goalsOpen[gid];
        renderGoals();
      } else if ((t = e.target.closest("[data-gcheck]"))) {
        goalsPost("/checkin", { id: parseInt(t.getAttribute("data-gcheck"), 10) });
      } else if ((t = e.target.closest("[data-gdone]"))) {
        var to = t.getAttribute("data-was") === "done" ? "active" : "done";
        goalsPost("/" + t.getAttribute("data-gdone"), { status: to }, "PATCH");
      } else if ((t = e.target.closest("[data-gstatus]"))) {
        goalsPost("/" + t.getAttribute("data-gstatus"), { status: t.getAttribute("data-to") }, "PATCH");
      } else if ((t = e.target.closest("[data-gdel]"))) {
        goalsPost("/" + t.getAttribute("data-gdel"), null, "DELETE");
      } else if ((t = e.target.closest("[data-gbreak]"))) {
        var bid = parseInt(t.getAttribute("data-gbreak"), 10);
        t.disabled = true; t.textContent = "Thinking...";
        fetch("/api/goals/breakdown", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: bid })
        }).then(parseJsonResponse).then(function (j) {
          if (j && j.suggestions && j.suggestions.length) goalsSug[bid] = j.suggestions;
          renderGoals();
        });
      } else if ((t = e.target.closest("[data-gsug]"))) {
        var sid = t.getAttribute("data-gsug"), idx = parseInt(t.getAttribute("data-i"), 10);
        var text = (goalsSug[sid] || [])[idx];
        if (text) {
          goalsSug[sid] = goalsSug[sid].filter(function (_, i) { return i !== idx; });
          if (!goalsSug[sid].length) delete goalsSug[sid];
          goalsPost("", { title: text, horizon: "week", parent_id: parseInt(sid, 10) });
        }
      } else if ((t = e.target.closest("[data-ghz]"))) {
        var sel = $("#gl-new-horizon");
        if (sel) sel.value = t.getAttribute("data-ghz");
        focusLater("#gl-new-title");
      }
    };
    box.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || !e.target) return;
      var add = e.target.getAttribute && e.target.getAttribute("data-gadd");
      if (add && e.target.value.trim()) {
        goalsPost("", { title: e.target.value.trim(), horizon: "week", parent_id: parseInt(add, 10) });
        e.target.value = "";
      } else if (e.target.id === "gl-new-title") {
        submitNewGoal();
      }
    });
    if ($("#gl-new-go")) $("#gl-new-go").onclick = submitNewGoal;
  }

  function submitNewGoal() {
    var title = $("#gl-new-title"), hz = $("#gl-new-horizon"), area = $("#gl-new-area");
    if (!title || !title.value.trim()) return;
    goalsPost("", { title: title.value.trim(), horizon: hz ? hz.value : "week",
                    area: area ? area.value : "" });
    title.value = "";
  }

  // ---------- RGB lighting ----------
  var rgbState = null, rgbTimer = null, rgbBusy = false, rgbNotice = "";
  var RGB_MODES = [
    ["off", "Off"], ["static", "Static"], ["breathe", "Breathe"], ["rainbow", "Rainbow"],
    ["temp", "Temp"], ["load", "Load"], ["ai", "AI"], ["goals", "Goals"]
  ];
  var RGB_MODE_HELP = {
    off: "Everything dark.",
    static: "One fixed colour.",
    breathe: "Chosen colour, fading in and out.",
    rainbow: "Continuous hue cycle.",
    temp: "Colour tracks the hottest of GPU/CPU — blue cool, red hot.",
    load: "Colour tracks GPU utilisation — blue idle, pink pegged.",
    ai: "Reacts to the rig: slow blue breath when idle, teal when a model is warm in VRAM, cyan-violet pulse while generating.",
    goals: "Colour tracks overall goal progress — red nowhere near, amber halfway, green done."
  };

  function rgbPaneActive() {
    var p = $("#sys-rgb");
    return activeTab === "system" && p && p.classList.contains("active");
  }

  function fetchRgb() {
    return fetch("/api/rgb").then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "lighting status failed"));
      rgbState = j;
      renderRgb();
      return j;
    }).catch(function (e) {
      rgbNotice = "Status unavailable: " + e.message;
      renderRgb();
    });
  }

  function startRgbPoll() {
    stopRgbPoll();
    rgbTimer = setInterval(function () {
      if (rgbPaneActive()) { if (!rgbBusy) fetchRgb(); }
      else stopRgbPoll();
    }, 2000);
  }
  function stopRgbPoll() { if (rgbTimer) { clearInterval(rgbTimer); rgbTimer = null; } }

  function rgbPost(path, body) {
    rgbBusy = true;
    rgbNotice = "Applying change...";
    renderRgb();
    return fetch("/api/rgb/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(parseJsonResponse).then(function (j) {
      if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "lighting change failed"));
      if (j && j.config) { rgbState = j; renderRgb(); }
      rgbNotice = "Saved.";
      return j;
    }).catch(function (e) {
      rgbNotice = "Could not apply change: " + e.message;
      renderRgb();
    }).finally(function () { rgbBusy = false; setTimeout(fetchRgb, 150); });
  }

  function rgbBadge(label, state, detail) {
    return '<span class="rgb-badge ' + esc(state) + '"><i></i>' + esc(label) +
      (detail ? '<small>' + esc(detail) + '</small>' : '') + '</span>';
  }

  function rgbDeviceCard(title, eyebrow, detail, on, health, actions, color, stateText) {
    return '<article class="rgb-device ' + (on ? 'on ' : '') + esc(health || "idle") + '">' +
      '<div class="rgb-device-top"><span class="rgb-device-light"' +
        (color ? ' style="--device-color:' + esc(color) + '"' : '') + '></span>' +
        '<span class="rgb-device-copy"><small>' + esc(eyebrow) + '</small><b>' + esc(title) +
        '</b></span><span class="rgb-device-state">' + esc(stateText || (on ? "LIVE" : health === "error" ? "ISSUE" : "IDLE")) + '</span></div>' +
      '<p>' + esc(detail) + '</p><div class="rgb-device-actions">' + actions + '</div></article>';
  }

  function renderRgb() {
    var box = $("#rgb-card");
    if (!box) return;
    if (!rgbState) {
      box.innerHTML = '<div class="gcard"><div class="sk-line sk-h"></div><div class="sk-line"></div><div class="sk-line"></div></div>';
      return;
    }
    var c = rgbState.config || {};
    var st = rgbState.status || {};
    var jrgb = rgbState.jrgb || {};
    var myst = rgbState.mystic || {};
    var kb = rgbState.keyboard || {};
    var live = st.last_color || c.color || "#000000";
    var modeItem = RGB_MODES.filter(function (m) { return m[0] === c.mode; })[0] || ["", c.mode || "static"];
    var aioOn = !!c.mystic;
    var aioHealth = !c.mystic ? "idle" : myst.available ? "ok" : "error";
    var boardHealth = !c.jrgb ? "idle" : jrgb.available ? "ok" : "error";
    var openHealth = st.connected ? "ok" : "error";
    var disabled = rgbBusy ? " disabled" : "";

    var h = '<div class="gcard rgb-console">' +
      '<div class="rgb-titlebar"><div><div class="card-h">Lighting studio</div>' +
      '<p>One scene, with the motherboard committed as one native MSI Center transaction.</p></div>' +
      '<label class="rgb-master"><span>ENGINE</span><input type="checkbox" id="rgb-enabled"' +
      (c.enabled ? ' checked' : '') + disabled + '><i></i></label></div>' +
      '<div class="rgb-health">' +
      rgbBadge("Motherboard", boardHealth, jrgb.latched ? "8 zones latched" : (jrgb.available ? "profile ready" : "offline")) +
      rgbBadge("AIO", aioHealth, aioOn ? (myst.latched ? "same transaction" : "attention") : "paused") +
      rgbBadge("OpenRGB", openHealth, st.connected ? "connected" : "offline") + '</div>';

    if (rgbState.hint) h += '<div class="rgb-warn">' + esc(rgbState.hint) + '</div>';

    h += '<section class="rgb-scene">' +
      '<div class="rgb-orb-wrap"><div class="rgb-orb" style="--rgb-live:' + esc(live) + '">' +
      '<span></span><b>' + esc(c.enabled ? modeItem[1] : "Off") + '</b><small>' + esc(live.toUpperCase()) + '</small></div></div>' +
      '<div class="rgb-scene-controls"><div class="rgb-section-label">Scene</div>' +
      '<div class="rgb-mode-grid" id="rgb-modes">';
    for (var i = 0; i < RGB_MODES.length; i++) {
      h += '<button' + disabled + ' class="rgb-mode' + (RGB_MODES[i][0] === c.mode ? ' on' : '') +
           '" data-rgbmode="' + RGB_MODES[i][0] + '"><span>' + RGB_MODES[i][1].slice(0, 1) +
           '</span><b>' + RGB_MODES[i][1] + '</b></button>';
    }
    h += '</div><p class="rgb-mode-help">' + esc(RGB_MODE_HELP[c.mode] || "") + '</p>' +
      '<div class="rgb-sliders"><label class="rgb-control"><span>Colour <b id="rgb-color-val">' +
      esc(c.color || "#4b9bff") + '</b></span><input id="rgb-color" type="color" value="' +
      esc(c.color || "#4b9bff") + '"' + disabled + '></label>' +
      '<label class="rgb-control"><span>Brightness <b id="rgb-bright-val">' + intgr(c.brightness) +
      '%</b></span><input id="rgb-bright" type="range" min="0" max="100" value="' +
      intgr(c.brightness) + '"' + disabled + '></label>' +
      '<label class="rgb-control"><span>Motion <b id="rgb-speed-val">' + intgr(c.speed) +
      '</b></span><input id="rgb-speed" type="range" min="1" max="100" value="' +
      intgr(c.speed) + '"' + disabled + '></label></div>' +
      '<div class="rgb-options"><label><input type="checkbox" id="rgb-alert"' + (c.alert ? ' checked' : '') +
      disabled + '> Thermal warning flash</label></div></div></section>';

    h += '<div class="rgb-section-head"><div><div class="rgb-section-label">Physical devices</div>' +
      '<h3>Lighting routes</h3></div><span>' + (rgbState.activity && rgbState.activity.length ?
      'Reactive activity: ' + esc(rgbState.activity.join(", ")) : 'No active override') + '</span></div>' +
      '<div class="rgb-device-grid">';

    var boardDetail = jrgb.available
      ? (jrgb.note || "Eight soldered zones are committed together through MSI Center and left latched.")
      : "MSI Center motherboard profile unavailable: " + (jrgb.error || "LEDKeeper2 did not acknowledge it");
    h += rgbDeviceCard("Motherboard LEDs", "MSI CENTER / ATOMIC 8-ZONE PROFILE", boardDetail,
      !!c.jrgb && !!jrgb.driven, boardHealth,
      '<button class="btn-sm' + (c.jrgb ? ' btn-ghost' : '') + '" data-rgbboard="' +
      (c.jrgb ? '0' : '1') + '"' + disabled + '>' + (c.jrgb ? 'Pause board' : 'Drive board') + '</button>',
      jrgb.last_color || live, c.jrgb ? (jrgb.latched ? "LATCHED" : "WAITING") : "PAUSED");

    var aioDetail = myst.available
      ? "The active JCORSAIR profile is included in the board's atomic commit—there is no second SDK pass to erase the onboard zones."
      : (c.mystic ? "MSI Center AIO profile needs attention: " + (myst.error || "not acknowledged") :
         "Paused. The JCORSAIR profile remains part of MSI Center, but is committed dark.");
    h += rgbDeviceCard("AIO pump + fans", "JCORSAIR / SAME TRANSACTION", aioDetail,
      aioOn && !!myst.latched, aioHealth,
      '<button class="btn-sm' + (aioOn ? ' btn-ghost' : '') + '" data-rgbaio="' +
      (aioOn ? '0' : '1') + '"' + disabled + '>' + (aioOn ? 'Pause AIO' : 'Drive AIO') + '</button>',
      myst.last_color || live, aioOn ? (myst.latched ? "LATCHED" : "WAITING") : "PAUSED");

    var devs = rgbState.devices || [], keyboardDev = null;
    for (var kd = 0; kd < devs.length; kd++) {
      if (/native HID/i.test(devs[kd].backend || "") || /EVision Keyboard/i.test(devs[kd].name || "")) keyboardDev = devs[kd];
    }
    if (keyboardDev || kb.available) {
      var keyboardName = (keyboardDev && keyboardDev.name) || "EVision Keyboard";
      h += rgbDeviceCard(keyboardName, "NATIVE HID", "Stored hardware effect; OpenRGB is bypassed so it cannot flash or revert.",
        !!c.keyboard && !!kb.available, kb.available ? "ok" : "error",
        '<button class="btn-sm' + (c.keyboard ? ' btn-ghost' : '') + '" data-rgbkeyboard="' +
        (c.keyboard ? '0' : '1') + '"' + disabled + '>' + (c.keyboard ? 'Pause keyboard' : 'Drive keyboard') + '</button>',
        kb.last_color || live);
    }

    for (var d = 0; d < devs.length; d++) {
      var dev = devs[d];
      if (dev === keyboardDev) continue;
      var devDetail = (dev.type_name || "OpenRGB device") + " / " + intgr(dev.leds) + " LED" +
        (intgr(dev.leds) === 1 ? "" : "s") + (dev.vendor ? " / " + dev.vendor : "");
      h += rgbDeviceCard(dev.name, "OPENRGB", devDetail, !!dev.driven, "ok",
        '<button class="btn-sm' + (dev.driven ? ' btn-ghost' : '') + '" data-rgbdev="' + esc(dev.name) +
        '" data-on="' + (dev.driven ? '0' : '1') + '"' + disabled + '>' +
        (dev.driven ? 'Pause' : 'Drive') + '</button><button class="btn-sm btn-ghost" data-rgbid="' +
        esc(dev.name) + '"' + disabled + '>Identify</button>', dev.last_color || live);
    }
    h += '</div>';

    h += '<details class="rgb-details"><summary>Controller details and safety</summary><div>' +
      '<p><b>Atomic MSI route:</b> LEDKeeper2 receives one complete <span class="mono">SettingData</span> profile containing all eight soldered motherboard zones and the JCORSAIR AIO profile. A change is healthy only after MSI Center clears its apply flag.</p>' +
      '<p><b>Why the public SDK is disabled:</b> on this MS-7B43, each ordinary per-zone call redraws the controller. Looping areas creates the two visible waves and leaves only the last subset lit; the advertised sync call returns unsupported.</p>' +
      '<p><b>Write policy:</b> identical static scenes perform zero writes. Animated scenes are coalesced to at most one complete profile transaction every ' + esc(myst.min_interval_s || 1.3) + 's.</p>' +
      '<p><b>OpenRGB:</b> ' + esc(st.server || "127.0.0.1:6742") + ' / ' + intgr(st.pushes) +
      ' engine writes. Peripherals remain opt-in.</p></div></details>' +
      '<div class="rgb-footer"><div class="rgb-acts"><button class="btn-sm" id="rgb-test"' + disabled +
      '>Run colour test</button><button class="btn-sm btn-ghost" id="rgb-rescan"' + disabled +
      '>Rescan devices</button></div><span class="rgb-notice' + (/Could not|unavailable/i.test(rgbNotice) ? ' error' : '') + '">' +
      esc(rgbNotice || (c.enabled ? "Scene is committed; static MSI colours require no keepalive." : "Engine is paused.")) + '</span></div></div>';
    box.innerHTML = h;
    wireRgb();
  }

  function wireRgb() {
    var modes = $("#rgb-modes");
    if (modes) modes.onclick = function (e) {
      var b = (e.target && e.target.closest) ? e.target.closest("[data-rgbmode]") : null;
      if (b) rgbPost("config", { mode: b.getAttribute("data-rgbmode") });
    };
    var color = $("#rgb-color");
    if (color) {
      color.oninput = function () { if ($("#rgb-color-val")) $("#rgb-color-val").textContent = this.value; };
      color.onchange = function () { rgbPost("config", { color: this.value }); };
    }
    var bright = $("#rgb-bright");
    if (bright) {
      bright.oninput = function () { if ($("#rgb-bright-val")) $("#rgb-bright-val").textContent = this.value + "%"; };
      bright.onchange = function () { rgbPost("config", { brightness: parseInt(this.value, 10) }); };
    }
    var speed = $("#rgb-speed");
    if (speed) {
      speed.oninput = function () { if ($("#rgb-speed-val")) $("#rgb-speed-val").textContent = this.value; };
      speed.onchange = function () { rgbPost("config", { speed: parseInt(this.value, 10) }); };
    }
    var en = $("#rgb-enabled");
    if (en) en.onchange = function () { rgbPost("config", { enabled: this.checked }); };
    var al = $("#rgb-alert");
    if (al) al.onchange = function () { rgbPost("config", { alert: this.checked }); };
    var box = $("#rgb-card");
    if (box) box.onclick = function (e) {
      if (!e.target || !e.target.closest) return;
      var board = e.target.closest("[data-rgbboard]");
      if (board) { rgbPost("config", { jrgb: board.getAttribute("data-rgbboard") === "1" }); return; }
      var aio = e.target.closest("[data-rgbaio]");
      if (aio) {
        var aioEnable = aio.getAttribute("data-rgbaio") === "1";
        rgbPost("config", { mystic: aioEnable });
        return;
      }
      var keyboard = e.target.closest("[data-rgbkeyboard]");
      if (keyboard) {
        rgbPost("config", { keyboard: keyboard.getAttribute("data-rgbkeyboard") === "1" });
        return;
      }
      var db = e.target.closest("[data-rgbdev]");
      if (db) {
        rgbPost("device", { name: db.getAttribute("data-rgbdev"), enabled: db.getAttribute("data-on") === "1" });
        return;
      }
      var ib = e.target.closest("[data-rgbid]");
      if (ib) {
        ib.disabled = true;
        rgbPost("identify", { name: ib.getAttribute("data-rgbid"), enabled: true });
        return;
      }
    };
    if ($("#rgb-test")) $("#rgb-test").onclick = function () {
      this.disabled = true; this.textContent = "Running...";
      rgbPost("test", {});
    };
    if ($("#rgb-rescan")) $("#rgb-rescan").onclick = function () {
      this.disabled = true;
      rgbBusy = true; rgbNotice = "Rescanning controllers..."; renderRgb();
      fetch("/api/rgb/rescan", { method: "POST" }).then(parseJsonResponse).then(function (j) {
        if (!j._httpOk || j.ok === false) throw new Error(responseError(j, "rescan failed"));
        rgbState = j; rgbNotice = "Rescan complete."; renderRgb();
      }).catch(function (e) { rgbNotice = "Could not rescan: " + e.message; renderRgb(); })
        .finally(function () { rgbBusy = false; renderRgb(); });
    };
  }

  // System tab sub-panes (GPU / CPU / Memory / Disks / Network / Processes / Lighting / Raw).
  function goSys(id) {
    $$("#sys-tabs .seg-btn").forEach(function (x) {
      x.classList.toggle("active", x.getAttribute("data-sys") === id);
    });
    $$(".sys-pane").forEach(function (p) {
      p.classList.toggle("active", p.id === "sys-" + id);
    });
    if (id === "rgb") { renderRgb(); fetchRgb(); startRgbPoll(); }
    else stopRgbPoll();
  }
  var sysTabs = $("#sys-tabs");
  if (sysTabs) sysTabs.addEventListener("click", function (e) {
    var b = (e.target && e.target.closest) ? e.target.closest(".seg-btn[data-sys]") : null;
    if (b) goSys(b.getAttribute("data-sys"));
  });

  // command palette controls
  if ($("#btn-cmdk")) $("#btn-cmdk").onclick = function () { cmdk.open ? closeCmdk() : openCmdk(); };
  if ($("#cmdk-backdrop")) $("#cmdk-backdrop").onclick = closeCmdk;
  var cmdkInput = $("#cmdk-input");
  if (cmdkInput) {
    cmdkInput.addEventListener("input", function () { cmdk.items = cmdkFilter(this.value); cmdk.sel = 0; renderCmdk(); });
    cmdkInput.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); cmdk.sel++; renderCmdk(); ensureCmdkVisible(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); cmdk.sel--; renderCmdk(); ensureCmdkVisible(); }
      else if (e.key === "Enter") { e.preventDefault(); runCmdkSel(); }
      else if (e.key === "Escape") { e.preventDefault(); closeCmdk(); }
    });
  }
  var cmdkList = $("#cmdk-list");
  if (cmdkList) cmdkList.addEventListener("click", function (e) {
    var it = (e.target && e.target.closest) ? e.target.closest(".cmdk-item[data-i]") : null;
    if (!it) return;
    cmdk.sel = parseInt(it.getAttribute("data-i"), 10) || 0;
    runCmdkSel();
  });
  function ensureCmdkVisible() {
    var list = $("#cmdk-list"); if (!list || !list.querySelectorAll) return;
    var sel = list.querySelectorAll(".cmdk-item")[cmdk.sel];
    if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: "nearest" });
  }
  if (window.addEventListener) window.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault(); cmdk.open ? closeCmdk() : openCmdk();
    } else if (e.key === "Escape" && cmdk.open) { closeCmdk(); }
  });

  // terminal controls
  if ($("#term-run")) $("#term-run").onclick = runTerm;
  if ($("#term-bg")) $("#term-bg").onclick = startTermBg;
  if ($("#term-restart")) $("#term-restart").onclick = restartService;
  if ($("#term-proc-refresh")) $("#term-proc-refresh").onclick = refreshProcs;
  var termCmd = $("#term-cmd");
  if (termCmd) termCmd.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); runTerm(); }
  });
  var termQuick = $("#term-quick");
  if (termQuick) termQuick.addEventListener("click", function (e) {
    var b = (e.target && e.target.closest) ? e.target.closest(".term-chip[data-cmd]") : null;
    if (!b) return;
    var inp = $("#term-cmd"); if (inp) inp.value = b.getAttribute("data-cmd");
    runTerm();
  });
  var termProcs = $("#term-procs");
  if (termProcs) termProcs.addEventListener("click", function (e) {
    var tl = (e.target && e.target.closest) ? e.target.closest("[data-tail]") : null;
    if (tl) { startTail(tl.getAttribute("data-tail")); return; }
    var sp = (e.target && e.target.closest) ? e.target.closest("[data-stop]") : null;
    if (sp) { stopProc(sp.getAttribute("data-stop")); return; }
  });

  // chat controls
  var ci = $("#chat-input");
  if (ci) {
    ci.addEventListener("input", function () {
      ci.style.height = "auto";
      ci.style.height = Math.min(ci.scrollHeight, 140) + "px";
    });
    ci.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });
  }
  if ($("#chat-send")) $("#chat-send").onclick = function () {
    if (chat.streaming) stopChat(); else sendChat();
  };
  if ($("#chat-model")) $("#chat-model").onchange = function () {
    chat.model = this.value;
    updateModelBadge();
    saveChat();
  };
  setAssistantMem(assistantMem); // reflect persisted state on the toggle
  if ($("#chat-mem")) $("#chat-mem").onclick = function () { setAssistantMem(!assistantMem); };
  setAutoSpeak(autoSpeak); // reflect persisted voice-toggle state
  if ($("#chat-voice")) $("#chat-voice").onclick = function () { setAutoSpeak(!autoSpeak); };
  if ($("#chat-new")) $("#chat-new").onclick = function () {
    if (chat.streaming) {
      chat.runId++;
      stopChat();
      chat.streaming = false;
      chat.controller = null;
      setSendState(false);
    }
    chat.messages = [];
    saveChat();
    showEmpty();
  };

  // studio controls
  var smode = $("#studio-mode");
  if (smode) smode.addEventListener("click", function (e) {
    var b = (e.target && e.target.closest) ? e.target.closest(".seg-btn[data-mode]") : null;
    if (b) studioSetMode(b.getAttribute("data-mode"));
  });
  var sliderIds = ["#studio-size", "#studio-quality", "#studio-length"];
  for (var sii = 0; sii < sliderIds.length; sii++) {
    var sEl = $(sliderIds[sii]);
    if (sEl) sEl.oninput = studioSyncLabels;
  }
  if ($("#studio-go")) $("#studio-go").onclick = studioGenerate;
  studioLoadHistory();
  renderStudioHistory();
  studioSyncLabels();

  // messaging controls
  if ($("#rig-agent-ask")) $("#rig-agent-ask").onclick = function () {
    askRig($("#rig-agent-q") && $("#rig-agent-q").value);
  };
  if ($("#rig-agent-q")) $("#rig-agent-q").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); askRig(this.value); }
  });
  if ($("#rig-agent-save")) $("#rig-agent-save").onclick = saveRigAgent;
  if ($("#rig-watch-save")) $("#rig-watch-save").onclick = saveRigWatch;
  if ($("#rig-agent-skills")) $("#rig-agent-skills").addEventListener("click", function (e) {
    var chip = (e.target && e.target.closest) ? e.target.closest("[data-rigskill]") : null;
    if (!chip) return;
    var q = "/" + chip.getAttribute("data-rigskill");
    var box = $("#rig-agent-q");
    if (box) box.value = q;
    askRig(q);
  });
  if ($("#messaging-refresh")) $("#messaging-refresh").onclick = function () { fetchMessagingStatus(true); };
  if ($("#messaging-send")) $("#messaging-send").onclick = sendMessagingTest;
  if ($("#autoreply-save")) $("#autoreply-save").onclick = saveAutoReply;
  if ($("#autoreply-test")) $("#autoreply-test").onclick = testAutoReply;
  if ($("#corrections-mine")) $("#corrections-mine").onclick = function () { fetchCorrections(true); };
  if ($("#messaging-provider")) $("#messaging-provider").onchange = updateMessagingWarning;
  if ($("#messaging-real-send")) $("#messaging-real-send").onchange = updateMessagingWarning;
  // digest controls
  if ($("#digest-add")) $("#digest-add").onclick = function () {
    var inp = $("#digest-add-input");
    var guid = (inp && inp.value || "").trim();
    if (!guid) return;
    var recs = digestRecipients();
    if (recs.indexOf(guid) < 0) recs.push(guid);
    if (inp) inp.value = "";
    saveDigest({ targets: recs, target: "" });
  };
  var digestBox = $("#digest-recipients");
  if (digestBox) digestBox.addEventListener("click", function (e) {
    var b = (e.target && e.target.closest) ? e.target.closest(".digest-x") : null;
    if (!b) return;
    var guid = b.getAttribute("data-guid");
    saveDigest({ targets: digestRecipients().filter(function (g) { return g !== guid; }), target: "" });
  });
  if ($("#digest-enabled")) $("#digest-enabled").onchange = function () {
    saveDigest({ enabled: !!$("#digest-enabled").checked });
  };
  if ($("#digest-hour")) $("#digest-hour").onchange = function () {
    saveDigest({ hour: parseInt($("#digest-hour").value, 10) || 8 });
  };
  if ($("#digest-preview")) $("#digest-preview").onclick = function () { runDigest(true); };
  if ($("#digest-send")) $("#digest-send").onclick = function () { runDigest(false); };

  // code controls
  if ($("#code-run")) $("#code-run").onclick = runCode;
  if ($("#code-refresh")) $("#code-refresh").onclick = fetchCodeInfo;
  var cmode = $("#code-mode");
  if (cmode) cmode.addEventListener("click", function (e) {
    var b = (e.target && e.target.closest) ? e.target.closest(".seg-btn[data-code-mode]") : null;
    if (b && !codeRun.streaming && !codeRun.busy) setCodeMode(b.getAttribute("data-code-mode"));
  });
  var codeTargetSel = $("#code-target");
  if (codeTargetSel) codeTargetSel.addEventListener("change", function () {
    codeRun.targetTouched = true;
    applyCodeTarget();
  });
  var codeTaskEl = $("#code-task");
  if (codeTaskEl) codeTaskEl.addEventListener("input", maybeAutoCodeTarget);
  var codeDeploy = $("#code-deploy");
  if (codeDeploy) codeDeploy.addEventListener("change", applyCodeTarget);
  $$(".mediaops-action").forEach(function (b) {
    b.onclick = function () { runCodeOp(b.getAttribute("data-op"), codeMediaPayload(codeTarget())); };
  });

  // AI hub action buttons (delegated)
  document.addEventListener("click", function (ev) {
    var t = ev.target;

    // Copy a code block from a chat message.
    var cp = (t && t.closest) ? t.closest(".md-copy[data-cb]") : null;
    if (cp) {
      var pre = document.getElementById(cp.getAttribute("data-cb"));
      if (pre) navigator.clipboard.writeText(pre.textContent).then(function () {
        cp.textContent = "Copied"; setTimeout(function () { cp.textContent = "Copy"; }, 1200);
      });
      return;
    }
    // Toggle the collapsible reasoning trace.
    var tt = (t && t.closest) ? t.closest(".think-toggle") : null;
    if (tt) {
      var tb = tt.parentNode.querySelector(".think-body");
      if (tb) {
        var open = tb.style.display !== "none";
        tb.style.display = open ? "none" : "";
        tt.innerHTML = (open ? "&#9656;" : "&#9662;") + " Reasoning";
      }
      return;
    }
    // Retry a failed assistant turn (its bubble was never committed to history).
    var rb = (t && t.closest) ? t.closest(".retry-btn") : null;
    if (rb) {
      var m = rb.closest(".msg");
      if (m && m.parentNode) m.parentNode.removeChild(m);
      streamAssistant();
      return;
    }

    var sc = (t && t.closest) ? t.closest(".svc-ctl[data-svc]") : null;
    if (sc && !sc.disabled) {
      ev.preventDefault();
      serviceAction(sc.getAttribute("data-svc"), sc.getAttribute("data-act"));
      return;
    }

    var mb = (t && t.closest) ? t.closest(".mdl-btn[data-act]") : null;
    if (mb && !mb.disabled) {
      hubAction(mb.getAttribute("data-model"), mb.getAttribute("data-act"));
      return;
    }
    var hd = (t && t.closest) ? t.closest(".hm-del[data-id]") : null;
    if (hd) {
      ev.preventDefault();
      hermesDelete(hd.getAttribute("data-id"));
      return;
    }
    var hp = (t && t.closest) ? t.closest(".hm-pinbtn[data-id]") : null;
    if (hp) {
      ev.preventDefault();
      hermesPinToggle(hp.getAttribute("data-id"), hp.getAttribute("data-pinned") === "1");
      return;
    }
    var hk = (t && t.closest) ? t.closest("[data-hkind]") : null;
    if (hk) {
      ev.preventDefault();
      hermesState.kindFilter = hk.getAttribute("data-hkind") || "";
      refreshHermesList();
      return;
    }
    var fr = (t && t.closest) ? t.closest(".fan-rescan-btn") : null;
    if (fr && !fr.disabled) {
      ev.preventDefault();
      fr.disabled = true; fr.textContent = "Scanning...";
      fetch("/api/fan/rescan", { method: "POST" }).then(parseJsonResponse).then(function (j) {
        var msg = fr.closest(".gcard") && fr.closest(".gcard").querySelector(".fan-msg");
        if (msg) {
          if (j && j._status === 404) {
            msg.textContent = "Backend is running old code — restart the service (_service\\revive.cmd).";
          } else if (j && j.ok === false) {
            msg.textContent = j.error || "still unavailable";
          }
        }
        fetchStats();
      }).catch(function () {}).then(function () {
        fr.disabled = false; fr.textContent = "Retry fan detection";
      });
      return;
    }
    var fsr = (t && t.closest) ? t.closest(".fan-restart-btn") : null;
    if (fsr) {
      ev.preventDefault();
      restartService();
      return;
    }
    var btn = (t && t.closest) ? t.closest(".sb[data-mode]") : null;
    if (!btn) return;
    var mode = btn.getAttribute("data-mode");
    var channelStr = btn.getAttribute("data-channel");
    var device = btn.getAttribute("data-device") || "cpu";
    if (!mode || channelStr == null) return;
    var channel = parseInt(channelStr, 10);
    var pendKey = device + ":" + channel;
    _fanPending[pendKey] = { mode: mode, until: Date.now() + 5000 };
    var card = btn.closest(".fan-card");
    if (card) {
      var kids = card.querySelectorAll(".sb");
      for (var i = 0; i < kids.length; i++) {
        kids[i].classList.toggle("on", kids[i] === btn);
      }
      var msg = card.querySelector(".fan-msg");
      if (msg) msg.textContent = "";
    }
    var cardSelector = '.fan-card[data-channel="' + channel + '"][data-device="' + device + '"]';
    fetch("/api/" + device + "/fan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode, channel: channel })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (!res.ok || (res.j && res.j.ok === false)) {
        _fanPending[pendKey] = null;
        var err = (res.j && res.j.error) ? res.j.error : "request failed";
        var card2 = document.querySelector(cardSelector);
        if (card2) {
          var m2 = card2.querySelector(".fan-msg");
          if (m2) m2.textContent = "Failed: " + err;
        }
      }
      fetchStats();
    }).catch(function () {
      _fanPending[pendKey] = null;
      var card3 = document.querySelector(cardSelector);
      if (card3) {
        var m3 = card3.querySelector(".fan-msg");
        if (m3) m3.textContent = "Network error";
      }
    });
  });

  // restore any saved conversation + normalize the send button
  loadChat();
  renderStoredMessages();
  setSendState(false);

  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    var tag = el.tagName.toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    return !!el.isContentEditable;
  }

  function updateViewportSize() {
    var root = document.documentElement;
    if (!root || !root.style || !root.style.setProperty) return;
    var viewport = window.visualViewport || null;
    var height = viewport && typeof viewport.height === "number" ? viewport.height : window.innerHeight;
    var width = viewport && typeof viewport.width === "number" ? viewport.width : window.innerWidth;
    var top = viewport && typeof viewport.offsetTop === "number" ? viewport.offsetTop : 0;
    var left = viewport && typeof viewport.offsetLeft === "number" ? viewport.offsetLeft : 0;
    // The shell is position:fixed and offset by the visual viewport so it stays put
    // while the iOS keyboard is up. Nothing else can scroll the layout viewport
    // (html/body are overflow:hidden), so a non-zero offset with no field focused is
    // always a stale value from a dismissed keyboard — clamp it or the whole console
    // stays pushed off-screen until the next resize event.
    if (!isTypingTarget(document.activeElement)) { top = 0; left = 0; }
    if (typeof height === "number" && isFinite(height) && height > 0) {
      root.style.setProperty("--app-height", Math.round(height) + "px");
    }
    if (typeof width === "number" && isFinite(width) && width > 0) {
      root.style.setProperty("--app-width", Math.round(width) + "px");
    }
    root.style.setProperty("--visual-top", Math.max(0, Math.round(top || 0)) + "px");
    root.style.setProperty("--visual-left", Math.max(0, Math.round(left || 0)) + "px");
  }

  function updateChromeHeight() {
    updateViewportSize();
    var root = document.documentElement;
    if (!root || !root.style || !root.style.setProperty) return;
    var hdr = $(".hdr");
    var hdrH = hdr && hdr.offsetHeight ? hdr.offsetHeight : 60;
    root.style.setProperty("--hdr-h", hdrH + "px");
  }
  updateChromeHeight();
  if (window.addEventListener) {
    window.addEventListener("resize", updateChromeHeight);
    window.addEventListener("orientationchange", function () {
      updateChromeHeight();
      if (window.setTimeout) window.setTimeout(updateChromeHeight, 250);
    });
    window.addEventListener("pageshow", updateChromeHeight);
    // iOS fires neither resize nor visualViewport.resize reliably when the keyboard
    // is dismissed by tapping away — re-measure after the blur animation settles.
    document.addEventListener("focusout", function () {
      if (!window.setTimeout) { updateChromeHeight(); return; }
      window.setTimeout(updateChromeHeight, 60);
      window.setTimeout(updateChromeHeight, 400);
    });
    document.addEventListener("focusin", updateChromeHeight);
  }
  if (window.visualViewport && window.visualViewport.addEventListener) {
    window.visualViewport.addEventListener("resize", updateChromeHeight);
    window.visualViewport.addEventListener("scroll", updateChromeHeight);
  }
  if (window.setTimeout) {
    window.setTimeout(updateChromeHeight, 0);
    window.setTimeout(updateChromeHeight, 500);
  }

  if (window.__RIG_SMOKE__) {
    window.__rigSmoke = {
      renderMarkdown: renderMarkdown,
      mergeChatEvent: mergeChatEvent,
      renderAihub: renderAihub,
      renderHermes: renderHermes,
      fetchHermes: fetchHermes,
      renderMessagingInit: renderMessagingInit,
      renderMedia: renderMedia,
      renderGpuFan: renderGpuFan,
      renderStudioStatus: renderStudioStatus,
      renderCodeInit: renderCodeInit,
      serviceAction: serviceAction,
      renderHeaderVitals: renderHeaderVitals,
      buildCmdkActions: buildCmdkActions,
      cmdkFilter: cmdkFilter,
      renderTerminalInit: renderTerminalInit,
      renderProcs: renderProcs,
      fetchRgb: fetchRgb,
      renderRgb: renderRgb,
      fetchGoals: fetchGoals,
      renderGoals: renderGoals,
      chatStatsHTML: chatStatsHTML,
      showEmpty: showEmpty,
      setGoalOpen: function (id) { goalsOpen[id] = true; },
      setProcs: function (v) { term.procs = v; },
      setMedia: function (value) { mediaState.out = value; }
    };
  }


  var siState = { owner: "", proposal: null };

  function siOwner() {
    return fetch("/api/rig-agent", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        siState.owner = ((j && j.owners) || [])[0] || "";
        return siState.owner;
      })
      .catch(function () { return ""; });
  }

  function siSay(text) {
    var out = $("#si-output");
    if (out) out.textContent = text;
  }

  function siTools(list) {
    var el = $("#si-tools");
    if (!el) return;
    if (!list || !list.length) { el.style.display = "none"; return; }
    el.style.display = "";
    el.textContent = "evidence: " + list.join(", ");
  }

  function siBusy(on, which) {
    var ids = ["#si-ask", "#si-propose", "#si-apply"];
    for (var i = 0; i < ids.length; i++) {
      var b = $(ids[i]);
      if (b) b.disabled = on;
    }
    var btn = $(which);
    if (btn) btn.textContent = on ? "working..." : btn.getAttribute("data-label");
  }

  function siPost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  function siAsk() {
    var q = ($("#si-q") && $("#si-q").value || "").trim();
    if (!q) { siSay("Ask something first."); return; }
    siBusy(true, "#si-ask");
    siSay("thinking...");
    siTools(null);
    $("#si-apply-wrap").style.display = "none";
    siState.proposal = null;
    siOwner().then(function (owner) {
      if (!owner) { siSay("No owner configured, so this is refused."); siBusy(false, "#si-ask"); return; }
      return siPost("/api/messaging/investigate", { question: q, sender: owner, steps: 5 })
        .then(function (j) {
          var used = ((j && j.evidence) || []).map(function (e) { return e.source || e.kind || "?"; });
          siTools(used);
          if (j && j.answer) siSay(j.answer);
          else siSay("state: " + ((j && j.state) || "?") + "\n" + ((j && j.reason) || "no answer"));
        });
    }).catch(function (e) { siSay("failed: " + e); })
      .then(function () { siBusy(false, "#si-ask"); });
  }

  function siPropose() {
    var q = ($("#si-q") && $("#si-q").value || "").trim();
    if (!q) { siSay("Describe what should change first."); return; }
    siBusy(true, "#si-propose");
    siSay("reading the code and writing a patch...");
    siTools(null);
    $("#si-apply-wrap").style.display = "none";
    siState.proposal = null;
    siOwner().then(function (owner) {
      if (!owner) { siSay("No owner configured, so this is refused."); siBusy(false, "#si-propose"); return; }
      return siPost("/api/messaging/propose-fix", { question: q, sender: owner, steps: 6 })
        .then(function (j) {
          var p = (j && j.proposal) || null;
          if (!p) { siSay("state: " + ((j && j.state) || "?") + "\n" + ((j && j.reason) || "no proposal")); return; }
          siState.proposal = p;
          // Field names come from app/propose_patch.py: patch, edits, plan,
          // risks, files, test.suite. Not diff/summary/suites.
          var parts = [];
          (p.files || []).forEach(function (f) {
            parts.push(f.path + (f.why ? " - " + f.why : "") +
                       (f.refused ? " [REFUSED: " + f.refused + "]" : ""));
          });
          if (p.plan && p.plan.length) parts.push("plan: " + p.plan.join(" -> "));
          if (p.risks && p.risks.length) parts.push("risks: " + p.risks.join("; "));
          if (p.test && p.test.suite) parts.push("suite it wants run: " + p.test.suite);
          parts.push("");
          parts.push(p.patch || "(no patch)");
          siSay(parts.join("\n"));
          // Applying writes, so it is a separate, deliberate click.
          if (p.edits && p.edits.length) $("#si-apply-wrap").style.display = "";
        });
    }).catch(function (e) { siSay("failed: " + e); })
      .then(function () { siBusy(false, "#si-propose"); });
  }

  function siApply() {
    var p = siState.proposal;
    if (!p || !p.edits || !p.edits.length) { siSay("Nothing to apply."); return; }
    siBusy(true, "#si-apply");
    siOwner().then(function (owner) {
      var suites = (p.test && p.test.suite) ? [p.test.suite] : [];
      return siPost("/api/messaging/apply-fix", {
        sender: owner, edits: p.edits, suites: suites,
        reason: (p.plan || []).join(" -> ") || "applied from the dashboard"
      }).then(function (j) {
        var lines = [];
        lines.push(j.applied ? "APPLIED" : "NOT APPLIED");
        if (j.reverted) lines.push("reverted: the files were put back");
        if (j.note) lines.push(j.note);
        if (j.checks && j.checks.failed_stage) lines.push("failed stage: " + j.checks.failed_stage);
        (j.suites || []).forEach(function (r) {
          lines.push((r.ok ? "ok   " : "FAIL ") + r.suite + "  " + (r.output || ""));
        });
        if (j.diff) lines.push("\n" + j.diff);
        siSay(lines.join("\n"));
        if (j.applied) $("#si-apply-wrap").style.display = "none";
      });
    }).catch(function (e) { siSay("failed: " + e); })
      .then(function () { siBusy(false, "#si-apply"); });
  }

  (function siInit() {
    var ask = $("#si-ask"), prop = $("#si-propose"), app2 = $("#si-apply"), q = $("#si-q");
    if (ask) { ask.setAttribute("data-label", "Ask"); ask.addEventListener("click", siAsk); }
    if (prop) { prop.setAttribute("data-label", "Propose fix"); prop.addEventListener("click", siPropose); }
    if (app2) { app2.setAttribute("data-label", "Apply this patch"); app2.addEventListener("click", siApply); }
    if (q) q.addEventListener("keydown", function (e) { if (e.key === "Enter") siAsk(); });
  })();

  function ccRiskBadge(on, label) {
    return '<span class="ca-tx" style="display:inline;margin-right:10px;color:'
         + (on ? "var(--good,#4caf50)" : "var(--muted,#888)") + '">'
         + label + (on ? " ✓" : " —") + "</span>";
  }

  function ccRow(c) {
    var mode = c.mode === "assistant" ? "self-chat (owner console)"
             : c.mode === "trusted_contact" ? "trusted contact"
             : "persona";
    var tools = (c.tools || []).join(", ") || "(none)";
    var note = c.contact_note
      ? '<div class="code-mode-help" style="margin-top:4px">note: '
        + esc(c.contact_note) + "</div>" : "";
    return '<div class="ca-row">'
      + '<div class="ca-tx"><b>' + esc(c.name || c.chat_guid) + '</b>'
      + '<span>' + mode + '</span>'
      + '<div style="margin-top:6px">'
      +   ccRiskBadge(c.allow_external_write_structural, "external write")
      +   ccRiskBadge(c.allow_privileged_structural, "privileged (code tools)")
      + '</div>'
      + '<div class="code-mode-help" style="margin-top:4px">tools: ' + esc(tools) + '</div>'
      + '<div class="code-mode-help">persona: ' + esc(c.persona_source || "") + '</div>'
      + note
      + '</div></div>';
  }

  function fetchChatControls() {
    var el = $("#cc-list");
    if (!el) return;
    el.innerHTML = '<div class="code-mode-help">loading…</div>';
    fetch("/api/messaging/chat-controls", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var chats = (j && j.chats) || [];
        if (!chats.length) { el.innerHTML = '<div class="code-mode-help">no configured chats found</div>'; return; }
        el.innerHTML = chats.map(ccRow).join("");
      })
      .catch(function (e) {
        el.innerHTML = '<div class="code-mode-help">failed to load: ' + esc(String(e)) + '</div>';
      });
  }

  if ($("#cc-refresh")) $("#cc-refresh").onclick = fetchChatControls;
  fetchChatControls();

  fetchHub();
  loop();
})();
