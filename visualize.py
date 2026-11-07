"""
Gate Management System -- visualizer.

Two ways to use it:

1. Interactive server (recommended for students):

       python visualize.py --serve

   Opens a page with a scenario dropdown and a solution-module box (defaults to
   "solution", i.e. your solution.py). Press Run and it executes your ACTUAL
   code -- freshly imported, so saved edits are picked up without restarting --
   against the chosen scenario, then renders gates as rows, flight occupancy as
   bars on a time axis, gate outages as red blocks, a play/pause "operational
   clock" playhead, and a decision-tick stepper. Switch scenarios from the same
   page; nothing is regenerated to disk.

2. Static export (for sharing a fixed result, e.g. in a report):

       python visualize.py --scenario flight_data/cascade_2.json --solution solution_kd --open
       python visualize.py --all --open

   Writes one self-contained HTML file per scenario into visualization/.

Both modes share the same renderer; the server just feeds it live data instead
of a value baked in at generation time.
"""

import argparse
import glob
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from evaluator import run_sim

OUT_DIR = "visualization"


# ---------------------------------------------------------------------------
# Shared trace + page generation
# ---------------------------------------------------------------------------
def list_scenarios(pattern: str = "flight_data/*.json") -> list:
    return sorted(Path(p).stem for p in glob.glob(pattern))


def _run_trace(static_path: str, scenario_path: str, solution_module: str) -> dict:
    result = run_sim(static_path, scenario_path, solution_module, verbose=False, record=True)
    trace = result["trace"]
    trace["result"] = {k: result.get(k) for k in
                       ("status", "score", "reason", "assigned", "unassigned", "breakdown")}
    trace["scenario_name"] = Path(scenario_path).stem
    return trace


def _render_html(mode: dict) -> str:
    return _TEMPLATE.replace("__MODE_JSON__", json.dumps(mode))


def generate(static_path: str, scenario_path: str, solution_module: str, out_path: str) -> tuple:
    trace = _run_trace(static_path, scenario_path, solution_module)
    html = _render_html({"server": False, "trace": trace})
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path, trace["result"]


# ---------------------------------------------------------------------------
# Interactive server
# ---------------------------------------------------------------------------
def _make_handler(static_path: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict, status: int = 200):
            self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

        def do_GET(self):  # noqa: N802 - stdlib method name
            parsed = urlsplit(self.path)
            qs = parse_qs(parsed.query)

            if parsed.path in ("/", "/index.html"):
                html = _render_html({"server": True, "trace": None})
                self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
                return

            if parsed.path == "/api/scenarios":
                self._send_json({"scenarios": list_scenarios()})
                return

            if parsed.path == "/api/run":
                scenario = (qs.get("scenario") or [""])[0]
                solution = (qs.get("solution") or ["solution"])[0]
                scenario_path = f"flight_data/{scenario}.json"

                if not scenario or not os.path.exists(scenario_path):
                    self._send_json({"error": f"Unknown scenario '{scenario}'."}, status=404)
                    return

                # Force a fresh import so saved edits to the student's module are
                # picked up without restarting this server.
                import sys
                sys.modules.pop(solution, None)

                try:
                    trace = _run_trace(static_path, scenario_path, solution)
                except ModuleNotFoundError as exc:
                    self._send_json({"error": f"Could not import '{solution}.py': {exc}"})
                except SyntaxError as exc:
                    self._send_json({"error": f"Syntax error in '{solution}.py': {exc}"})
                except Exception as exc:  # noqa: BLE001 - surface any failure to the page
                    self._send_json({"error": f"{type(exc).__name__}: {exc}"})
                else:
                    self._send_json({"trace": trace})
                return

            self._send(404, "text/plain; charset=utf-8", b"not found")

        def log_message(self, fmt, *args):  # quieter than the default access log
            pass

    return Handler


def serve(static_path: str = "static_info.json", host: str = "127.0.0.1",
         port: int = 8765, open_browser: bool = True):
    handler = _make_handler(static_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"GMS visualizer serving at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="GMS visualizer")
    parser.add_argument("--scenario", default="flight_data/simple.json")
    parser.add_argument("--static", default="static_info.json")
    parser.add_argument("--solution", default="solution_scored")
    parser.add_argument("--out", default=None, help="output HTML path")
    parser.add_argument("--all", action="store_true", help="render every flight_data/*.json to disk")
    parser.add_argument("--open", dest="open_browser", action="store_true",
                        help="open the result(s) in a browser")
    parser.add_argument("--serve", action="store_true",
                        help="launch the interactive server instead of writing files")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.static, args.host, args.port, open_browser=True)
        return 0

    scenarios = sorted(glob.glob("flight_data/*.json")) if args.all else [args.scenario]
    produced = []
    for scenario in scenarios:
        name = Path(scenario).stem
        out = args.out if (args.out and not args.all) else os.path.join(OUT_DIR, f"{name}__{args.solution}.html")
        path, result = generate(args.static, scenario, args.solution, out)
        print(f"  {name:<18} {result['status']:<8} -> {path}")
        produced.append(path)

    if args.open_browser:
        for path in produced:
            webbrowser.open(Path(path).resolve().as_uri())
    return 0


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GMS Visualizer</title>
<style>
  :root { --labelw: 168px; --ruler: 30px; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0e1116; color: #e6edf3;
         font: 14px/1.45 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
  header { padding: 16px 22px; border-bottom: 1px solid #232a33;
           display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; font-weight: 650; }
  header .sub { color: #8b949e; font-size: 13px; }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
  .badge.ok { background: #1a3326; color: #3fb950; }
  .badge.fail { background: #3a1d22; color: #f85149; }
  .wrap { padding: 18px 22px 40px; }
  .toolbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
             background: #161b22; border: 1px solid #232a33; border-radius: 10px;
             padding: 12px 16px; margin-bottom: 16px; }
  .toolbar button { background: #21262d; color: #e6edf3; border: 1px solid #30363d;
                    border-radius: 7px; padding: 7px 13px; cursor: pointer; font-size: 13px; }
  .toolbar button:hover { background: #2b333c; }
  .toolbar button:disabled { opacity: .5; cursor: default; }
  .toolbar button.primary { background: #238636; border-color: #2ea043; font-weight: 650; }
  .toolbar select, .toolbar input[type=text] {
    background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 7px;
    padding: 6px 10px; font-size: 13px; font-family: ui-monospace, Consolas, monospace;
  }
  .toolbar .group { display: flex; align-items: center; gap: 8px; }
  .toolbar .sep { width: 1px; height: 26px; background: #30363d; }
  .clock { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 16px; min-width: 70px; }
  .muted { color: #8b949e; font-size: 12px; }
  input[type=range] { accent-color: #2ea043; }

  .runbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
            background: #161b22; border: 1px solid #2ea043; border-radius: 10px;
            padding: 12px 16px; margin-bottom: 16px; }
  .runbar label { font-size: 12px; color: #8b949e; font-weight: 650; text-transform: uppercase; letter-spacing: .04em; }
  .errorBox { display: none; background: #2a1216; border: 1px solid #f85149; color: #ffb4ad;
              border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; font-size: 13px;
              font-family: ui-monospace, Consolas, monospace; white-space: pre-wrap; }

  .chart { position: relative; background: #0d1117; border: 1px solid #232a33;
           border-radius: 10px; padding: 0 0 8px; overflow: hidden; min-height: 80px; }
  .placeholder { padding: 30px; text-align: center; color: #6e7681; }
  .ruler { display: flex; height: var(--ruler); border-bottom: 1px solid #232a33; position: relative; }
  .ruler .spacer { width: var(--labelw); flex: 0 0 var(--labelw); }
  .ruler .track { flex: 1; position: relative; }
  .ruler .hr { position: absolute; top: 0; bottom: 0; transform: translateX(-50%);
               font-size: 11px; color: #6e7681; padding-top: 8px; }
  .row { display: flex; height: 38px; border-bottom: 1px solid #161b22; }
  .row .label { width: var(--labelw); flex: 0 0 var(--labelw); display: flex; align-items: center;
                gap: 8px; padding: 0 10px; border-right: 1px solid #232a33; border-left: 4px solid transparent; }
  .row .label .gid { font-weight: 700; }
  .row .label .chip { font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px; }
  .row .label .status { margin-left: auto; font-size: 11px; color: #6e7681; white-space: nowrap; }
  .row.inuse { background: #11161d; }
  .row.down  { background: #1c1416; }
  .track { flex: 1; position: relative; }
  .bar { position: absolute; top: 6px; height: 26px; border-radius: 5px; overflow: hidden;
         display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 650;
         color: #0d1117; white-space: nowrap; box-shadow: inset 0 0 0 1px rgba(0,0,0,.25); }
  .bar.live { outline: 2px solid #fff; outline-offset: -1px; }
  .outage { position: absolute; top: 4px; height: 30px; border-radius: 5px;
            background: repeating-linear-gradient(45deg, #5a1d22, #5a1d22 6px, #7a262d 6px, #7a262d 12px);
            border: 1px solid #f85149; color: #ffb4ad; font-size: 10px; font-weight: 700;
            display: flex; align-items: center; justify-content: center; }

  .overlay { position: absolute; left: 0; right: 0; top: var(--ruler); bottom: 8px; pointer-events: none; }
  .grid { position: absolute; top: 0; bottom: 0; width: 1px; background: #1b222b; }
  .playhead { position: absolute; top: 0; bottom: 0; width: 2px; background: #58a6ff;
              box-shadow: 0 0 8px #58a6ff; }

  .panels { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 16px; }
  .panel { background: #161b22; border: 1px solid #232a33; border-radius: 10px; padding: 12px 16px; }
  .panel h3 { margin: 0 0 8px; font-size: 13px; color: #8b949e; font-weight: 650; text-transform: uppercase; letter-spacing: .04em; }
  .log { font-family: ui-monospace, Consolas, monospace; font-size: 12px; white-space: pre-wrap; color: #c9d1d9; }
  .log .o { color: #f0883e; } .log .a { color: #3fb950; } .log .r { color: #58a6ff; }
  .log .c { color: #f85149; } .log .u { color: #8b949e; }
  .kv { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px solid #1b222b; }
  .scroll { max-height: 260px; overflow: auto; }
  .count { font-size: 11px; color: #8b949e; font-weight: 600; margin-bottom: 6px; }
  .qrow { display: flex; align-items: center; gap: 8px; padding: 4px 0; border-bottom: 1px solid #1b222b; font-size: 12px; }
  .fchip { color: #0d1117; font-weight: 700; border-radius: 4px; padding: 1px 7px; min-width: 46px; text-align: center; }
  .qcat { font-size: 10px; font-weight: 700; width: 36px; }
  .qarrow { font-weight: 600; flex: 1; }
  .qarrow.a { color: #3fb950; } .qarrow.r { color: #58a6ff; } .qarrow.u { color: #6e7681; }
  .qgate { font-weight: 700; padding: 1px 8px; border-radius: 4px; background: #21262d; }

  .banner { background: #161b22; border: 1px solid #30363d; border-left: 4px solid #f0883e;
            border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; font-size: 13px;
            display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
  .banner .ev { font-weight: 650; }
  .banner .ev.o { color: #f0883e; } .banner .ev.r { color: #58a6ff; } .banner .ev.c { color: #f85149; }
  @keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(88,166,255,.85); } 50% { box-shadow: 0 0 0 5px rgba(88,166,255,0); } }
  .bar.moved { outline: 2px solid #58a6ff; outline-offset: -1px; animation: pulse 1.4s infinite; z-index: 3; }
  .bar.ghost { opacity: .4; background: #30363d !important; border: 1px dashed #8b949e; color: #8b949e; z-index: 1; }
  .bar.emergency { box-shadow: inset 0 0 0 2px #f85149, 0 0 7px rgba(248,81,73,.55); font-weight: 800; }
  .legend { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 14px; color: #8b949e; font-size: 12px; align-items: center; }
  .legend .sw { display: inline-block; width: 14px; height: 14px; border-radius: 3px; vertical-align: -2px; margin-right: 5px; }
  .dis-entry { padding: 6px 0; border-bottom: 1px solid #1b222b; }
  .dis-entry .dis-time { color: #8b949e; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 2px; }
  .dis-entry .dis-line { display: block; font-family: ui-monospace, Consolas, monospace; font-size: 12px; padding: 1px 0; color: #c9d1d9; }
  .dis-entry .dis-line.o { color: #f0883e; } .dis-entry .dis-line.c { color: #f85149; }
  .dis-entry .dis-line.r { color: #58a6ff; } .dis-entry .dis-line.u { color: #8b949e; }
</style>
</head>
<body>
<header>
  <h1 id="title">Gate Management System</h1>
  <span class="sub" id="subtitle"></span>
  <span id="result" class="badge"></span>
</header>

<div class="wrap">
  <div id="runbar" class="runbar" style="display:none">
    <div class="group">
      <label>Scenario</label>
      <select id="scenarioSelect"></select>
    </div>
    <button id="runBtn" class="primary">&#9654; Run</button>
    <span class="muted" id="runStatus"></span>
  </div>

  <div id="errorBox" class="errorBox"></div>

  <div class="toolbar">
    <div class="group">
      <button id="play" class="primary">&#9654; Play</button>
      <button id="reset">&#8634; Reset</button>
      <span class="clock" id="clock">--:--</span>
    </div>
    <div class="group" style="flex:1; min-width:240px;">
      <span class="muted">clock</span>
      <input type="range" id="scrub" min="0" max="1000" value="0" style="flex:1;">
    </div>
    <div class="group">
      <span class="muted">speed</span>
      <select id="speed">
        <option value="4">0.1x</option>
        <option value="10">0.25x</option>
        <option value="20">0.5x</option>
        <option value="40" selected>1x</option>
        <option value="80">2x</option>
        <option value="160">4x</option>
      </select>
    </div>
    <div class="sep"></div>
    <div class="group">
      <span class="muted">plan&nbsp;@</span>
      <button id="tprev">&#9664;</button>
      <span class="clock" id="tickLabel" style="font-size:14px; min-width:90px; text-align:center;"></span>
      <button id="tnext">&#9654;</button>
      <button id="playDec" class="primary">&#9654; Decisions</button>
    </div>
  </div>

  <div id="banner" class="banner" style="display:none"></div>

  <div class="chart" id="chart"><div class="placeholder" id="chartPlaceholder">Pick a scenario and press Run.</div></div>

  <div class="legend" id="legend"></div>

  <div class="panels">
    <div class="panel">
      <h3>Flight queue @ <span id="tickLabel3"></span></h3>
      <div class="scroll" id="queue"></div>
    </div>
    <div class="panel">
      <h3>Events at plan tick <span id="tickLabel2"></span></h3>
      <div class="log scroll" id="log"></div>
    </div>
    <div class="panel">
      <h3>Gate status at <span id="clock2">--:--</span></h3>
      <div class="scroll" id="status"></div>
    </div>
    <div class="panel" style="grid-column: 1 / -1">
      <h3>Disruption &amp; response timeline</h3>
      <div class="scroll" id="disruptionLog" style="max-height: 200px"></div>
    </div>
  </div>
</div>

<script>
const MODE = __MODE_JSON__;
const SERVER_MODE = !!MODE.server;

let TRACE = null;
let TMIN = 0, TMAX = 1, SPAN = 1, OPEN = Infinity;
const TYPE_COLOR = {0: "#8b949e", 1: "#58a6ff", 2: "#3fb950"};
const TYPE_SHORT = {0: "CARGO", 1: "DOM", 2: "INTL"};

let planIdx = 0;   // start on the initial plan; press "Decisions" to play the day forward
let clock = 0;
let playing = false;
let raf = null, lastTs = null;
let decTimer = null;

function pad(n){ return String(n).padStart(2,"0"); }
function fmt(min){ min = Math.round(min); const d = Math.floor(min/1440), m = ((min%1440)+1440)%1440;
  const s = pad(Math.floor(m/60))+":"+pad(m%60); return d>0 ? s+"+"+d+"d" : s; }
function frac(t){ return (t - TMIN) / SPAN; }
function shortId(fid){ const p = fid.split("-"); return p.length>=2 ? p[0]+p[1] : fid; }
function colorFor(fid){ let h=0; for(const c of fid) h=(h*31 + c.charCodeAt(0))>>>0; return `hsl(${h%360} 62% 58%)`; }
function clampEnd(e){ return e >= OPEN ? TMAX : e; }

// ---- load a trace (fresh page-load, or a new Run) --------------------------
function loadTrace(trace){
  TRACE = trace;
  TMIN = trace.time_min; TMAX = trace.time_max; SPAN = Math.max(1, TMAX - TMIN);
  OPEN = trace.outage_open_end;
  planIdx = 0; clock = TMIN; playing = false;
  if(decTimer){ clearInterval(decTimer); decTimer = null; }
  document.getElementById("playDec").innerHTML = "&#9654; Decisions";
  document.getElementById("chart").innerHTML = "";
  buildStatic();
  renderPlan();
  updateClock(TMIN);
}

// ---- static scaffold (rebuilt for every trace) ------------------------------
function buildStatic(){
  document.getElementById("title").textContent = "Gate Management System — " + TRACE.scenario_name;
  document.getElementById("subtitle").textContent = "solution: " + TRACE.solution;
  const r = TRACE.result, badge = document.getElementById("result");
  if(r.status === "OK"){ badge.className = "badge ok";
    badge.textContent = "COMPLETED · score " + (r.score==null?"-":r.score.toFixed(2)); }
  else { badge.className = "badge fail"; badge.textContent = "HARD FAILURE: " + (r.reason||""); }

  const chart = document.getElementById("chart");
  // ruler
  const ruler = document.createElement("div"); ruler.className = "ruler";
  const sp = document.createElement("div"); sp.className = "spacer"; ruler.appendChild(sp);
  const rtrack = document.createElement("div"); rtrack.className = "track";
  for(let h = Math.ceil(TMIN/60)*60; h <= TMAX; h += 60){
    const hr = document.createElement("div"); hr.className = "hr";
    hr.style.left = (frac(h)*100) + "%"; hr.textContent = fmt(h); rtrack.appendChild(hr);
  }
  ruler.appendChild(rtrack); chart.appendChild(ruler);

  // gate rows
  for(const g of TRACE.gates){
    const row = document.createElement("div"); row.className = "row"; row.dataset.gid = g.id;
    const label = document.createElement("div"); label.className = "label";
    label.style.borderLeftColor = TYPE_COLOR[g.type];
    label.title = `max wingspan ${g.max_wingspan}m · jetbridge ${g.jetbridge?"yes":"no"} · dist ${g.dist}`;
    label.innerHTML = `<span class="gid">${g.id}</span>`
      + `<span class="chip" style="background:${TYPE_COLOR[g.type]}22;color:${TYPE_COLOR[g.type]}">${TYPE_SHORT[g.type]}</span>`
      + `<span class="status" data-status></span>`;
    const track = document.createElement("div"); track.className = "track"; track.dataset.track = g.id;
    row.appendChild(label); row.appendChild(track); chart.appendChild(row);
  }

  // overlay (gridlines + playhead)
  const overlay = document.createElement("div"); overlay.className = "overlay"; overlay.id = "overlay";
  for(let h = Math.ceil(TMIN/60)*60; h <= TMAX; h += 60){
    const gl = document.createElement("div"); gl.className = "grid";
    gl.style.left = `calc(var(--labelw) + (100% - var(--labelw)) * ${frac(h)})`;
    overlay.appendChild(gl);
  }
  const ph = document.createElement("div"); ph.className = "playhead"; ph.id = "playhead";
  overlay.appendChild(ph); chart.appendChild(overlay);

  // legend
  document.getElementById("legend").innerHTML =
    `<span><span class="sw" style="background:#58a6ff"></span>domestic gate</span>`
    + `<span><span class="sw" style="background:#3fb950"></span>international gate</span>`
    + `<span><span class="sw" style="background:#8b949e"></span>cargo gate</span>`
    + `<span><span class="sw" style="background:repeating-linear-gradient(45deg,#7a262d,#7a262d 4px,#5a1d22 4px,#5a1d22 8px);border:1px solid #f85149"></span>gate outage</span>`
    + `<span><span class="sw" style="outline:2px solid #fff;background:#444"></span>occupied right now</span>`
    + `<span>&#9879; emergency / diverted flight</span>`;
}

// ---- render the plan for the selected decision tick ------------------------
function renderPlan(){
  const tick = TRACE.ticks[planIdx];

  // Snapshot every bar's screen position before we wipe tracks (for FLIP animation)
  const oldPos = {};
  document.querySelectorAll('.bar[data-fid]').forEach(bar => {
    if(!(bar.dataset.fid in oldPos)){
      const r = bar.getBoundingClientRect();
      oldPos[bar.dataset.fid] = {left: r.left, top: r.top};
    }
  });
  const movedSet = new Set((tick.decisions.reassignments||[]).map(([f]) => f));

  const phase = (planIdx === 0) ? "initial plan @ " : "plan @ ";
  document.getElementById("tickLabel").textContent = phase + tick.time_label;
  document.getElementById("tickLabel2").textContent = tick.time_label + (planIdx === 0 ? " (initial plan)" : "");
  for(const tr of document.querySelectorAll("[data-track]")) tr.innerHTML = "";

  for(const gid in tick.plan){
    const tr = document.querySelector(`[data-track="${gid}"]`); if(!tr) continue;
    for(const b of tick.plan[gid]){
      for(const iv of b.intervals){
        const bar = document.createElement("div"); bar.className = "bar";
        if(b.diversion) bar.classList.add("emergency");
        bar.style.left = (frac(iv[0])*100) + "%";
        bar.style.width = Math.max(0.4, (iv[1]-iv[0])/SPAN*100) + "%";
        bar.style.background = colorFor(b.flight_id);
        bar.dataset.s = iv[0]; bar.dataset.e = iv[1]; bar.dataset.fid = b.flight_id;
        bar.textContent = (b.diversion ? "⚕ " : "") + shortId(b.flight_id);
        bar.title = `${shortId(b.flight_id)} @ ${gid}  [${fmt(iv[0])} - ${fmt(iv[1])}]`;
        tr.appendChild(bar);
      }
    }
  }
  for(const gid in tick.outages){
    const tr = document.querySelector(`[data-track="${gid}"]`); if(!tr) continue;
    for(const w of tick.outages[gid]){
      const end = clampEnd(w[1]);
      const o = document.createElement("div"); o.className = "outage";
      o.style.left = (frac(w[0])*100) + "%";
      o.style.width = Math.max(0.4, (end-w[0])/SPAN*100) + "%";
      o.dataset.s = w[0]; o.dataset.e = end; o.textContent = "OUT";
      o.title = `outage [${fmt(w[0])} - ${fmt(w[1])}]`;
      tr.appendChild(o);
    }
  }

  const prevPlan = planIdx > 0 ? TRACE.ticks[planIdx-1].plan : {};
  const prevGate = {};
  for(const g in prevPlan) prevPlan[g].forEach(b => prevGate[b.flight_id] = g);
  (tick.decisions.reassignments||[]).forEach(([f, g]) => {
    document.querySelectorAll(`[data-track="${g}"] .bar`).forEach(bar => {
      if(bar.dataset.fid === f) bar.classList.add("moved");
    });
    const og = prevGate[f];
    const pb = og && (prevPlan[og]||[]).find(b => b.flight_id === f);
    if(pb){
      const tr = document.querySelector(`[data-track="${og}"]`);
      pb.intervals.forEach(iv => {
        const gh = document.createElement("div"); gh.className = "bar ghost";
        gh.style.left = (frac(iv[0])*100) + "%";
        gh.style.width = Math.max(0.4, (iv[1]-iv[0])/SPAN*100) + "%";
        gh.textContent = shortId(f);
        gh.title = `${shortId(f)} was at ${og} before ${tick.time_label}`;
        tr.appendChild(gh);
      });
    }
  });

  renderBanner(tick);
  renderQueue(tick);
  renderLog(tick.log);
  updateClock(clock);

  // FLIP animation: slide reassigned bars from their old gate row to their new position
  if(movedSet.size){
    requestAnimationFrame(() => {
      document.querySelectorAll('.bar.moved[data-fid]').forEach(bar => {
        const fid = bar.dataset.fid;
        if(!movedSet.has(fid) || !oldPos[fid]) return;
        const r = bar.getBoundingClientRect();
        const dx = oldPos[fid].left - r.left;
        const dy = oldPos[fid].top - r.top;
        if(Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
        bar.style.transform = `translate(${dx}px, ${dy}px)`;
        bar.style.transition = 'none';
        requestAnimationFrame(() => {
          bar.style.transition = 'transform 0.55s cubic-bezier(.25,.46,.45,.94)';
          bar.style.transform = '';
        });
      });
    });
  }
  renderDisruptionLog();
}

function renderBanner(tick){
  const evs = (tick.log||[]).filter(L => /\[(OUTAGE|REASSIGN|CANCEL|DIVERT)\]/.test(L));
  const b = document.getElementById("banner");
  if(!evs.length){ b.style.display = "none"; return; }
  b.style.display = "flex";
  b.innerHTML = `<span class="muted">at ${tick.time_label}</span>` + evs.map(L => {
    let cls = "c", icon = "✕";
    if(L.includes("[OUTAGE]")) { cls = "o"; icon = "⚠"; }
    else if(L.includes("[REASSIGN]")) { cls = "r"; icon = "↺"; }
    else if(L.includes("[DIVERT]")) { cls = "o"; icon = "⚕"; }
    const txt = L.replace(/^\s*\[[A-Z]+\]\s*/, "").replace(/&/g,"&amp;").replace(/</g,"&lt;");
    return `<span class="ev ${cls}">${icon} ${txt}</span>`;
  }).join("");
}

function qrow(fid, label, gate, cls, cat){
  const c = (cat==null) ? "" : `<span class="qcat" style="color:${TYPE_COLOR[cat]}">${TYPE_SHORT[cat]}</span>`;
  return `<div class="qrow"><span class="fchip" style="background:${colorFor(fid)}">${shortId(fid)}</span>`
    + c + `<span class="qarrow ${cls}">${label}</span><span class="qgate">${gate}</span></div>`;
}

function renderQueue(tick){
  document.getElementById("tickLabel3").textContent = tick.time_label;
  const cats = {};
  (tick.queue||[]).forEach(q => cats[q.flight_id] = q.category);
  for(const g in tick.plan) tick.plan[g].forEach(b => cats[b.flight_id] = b.category);

  const placed = new Set(), rows = [];
  (tick.decisions.assignments||[]).forEach(([f,g]) => { placed.add(f); rows.push(qrow(f, "assigned →", g, "a", cats[f])); });
  (tick.decisions.reassignments||[]).forEach(([f,g]) => rows.push(qrow(f, "moved ↺", g, "r", cats[f])));
  (tick.queue||[]).forEach(q => { if(!placed.has(q.flight_id)) rows.push(qrow(q.flight_id, "waiting", ". . .", "u", q.category)); });

  const nA = (tick.decisions.assignments||[]).length, nR = (tick.decisions.reassignments||[]).length;
  document.getElementById("queue").innerHTML =
    `<div class="count">${(tick.queue||[]).length} in queue &middot; ${nA} assigned &middot; ${nR} moved</div>`
    + (rows.length ? rows.join("") : '<div class="muted">no flights to place this tick</div>');
}

function renderLog(lines){
  const el = document.getElementById("log");
  el.innerHTML = lines.map(L => {
    let cls = "";
    if(L.includes("[OUTAGE]")) cls="o"; else if(L.includes("[ASSIGN]")) cls="a";
    else if(L.includes("[REASSIGN]")) cls="r"; else if(L.includes("[CANCEL]")) cls="c";
    else if(L.includes("[UPDATE]")||L.includes("[WARN]")) cls="u";
    const safe = L.replace(/&/g,"&amp;").replace(/</g,"&lt;");
    return `<span class="${cls}">${safe}</span>`;
  }).join("\n");
}

function renderDisruptionLog(){
  const el = document.getElementById("disruptionLog");
  if(!el || !TRACE) return;
  const entries = [];
  for(let i = 0; i <= planIdx; i++){
    const tick = TRACE.ticks[i];
    const lines = (tick.log||[]).filter(L =>
      /\[(OUTAGE|CANCEL|DIVERT|UPDATE|REASSIGN)\]/.test(L)
    );
    if(!lines.length) continue;
    const lineHtml = lines.map(L => {
      let cls = "";
      if(L.includes("[OUTAGE]")) cls = "o";
      else if(L.includes("[CANCEL]")) cls = "c";
      else if(L.includes("[DIVERT]")) cls = "o";
      else if(L.includes("[REASSIGN]")) cls = "r";
      else if(L.includes("[UPDATE]")) cls = "u";
      const safe = L.replace(/&/g,"&amp;").replace(/</g,"&lt;");
      return `<span class="dis-line ${cls}">${safe}</span>`;
    }).join("");
    entries.push(`<div class="dis-entry"><div class="dis-time">@ ${tick.time_label}</div>${lineHtml}</div>`);
  }
  el.innerHTML = entries.length
    ? entries.join("")
    : '<span class="muted" style="font-size:12px">No disruptions yet.</span>';
  el.scrollTop = el.scrollHeight;
}

// ---- clock: move playhead, mark gates in-use / free / down -----------------
function updateClock(t){
  clock = t;
  document.getElementById("clock").textContent = fmt(t);
  document.getElementById("clock2").textContent = fmt(t);
  document.getElementById("scrub").value = Math.round(frac(t)*1000);
  document.getElementById("playhead").style.left =
    `calc(var(--labelw) + (100% - var(--labelw)) * ${frac(t)})`;

  const tick = TRACE.ticks[planIdx];
  const statusRows = [];
  for(const g of TRACE.gates){
    const row = document.querySelector(`.row[data-gid="${g.id}"]`);
    const statusEl = row.querySelector("[data-status]");
    row.classList.remove("inuse","down");
    let occupant = null, down = false;
    (tick.plan[g.id]||[]).forEach(b => b.intervals.forEach(iv => {
      if(t >= iv[0] && t < iv[1]) occupant = shortId(b.flight_id);
    }));
    (tick.outages[g.id]||[]).forEach(w => { if(t >= w[0] && t < clampEnd(w[1])) down = true; });

    row.querySelectorAll(".bar").forEach(bar => {
      const live = t >= +bar.dataset.s && t < +bar.dataset.e;
      bar.classList.toggle("live", live);
    });

    if(down){ row.classList.add("down"); statusEl.textContent = "OUTAGE"; statusEl.style.color = "#f85149"; }
    else if(occupant){ row.classList.add("inuse"); statusEl.textContent = "● " + occupant; statusEl.style.color = "#3fb950"; }
    else { statusEl.textContent = "free"; statusEl.style.color = "#6e7681"; }
    statusRows.push(`<div class="kv"><span>${g.id} <span class="muted">(${TYPE_SHORT[g.type]})</span></span>`
      + `<span style="color:${down?"#f85149":occupant?"#3fb950":"#6e7681"}">${down?"outage":occupant?occupant:"free"}</span></div>`);
  }
  document.getElementById("status").innerHTML = statusRows.join("");
}

// ---- playback --------------------------------------------------------------
function step(ts){
  if(!playing) return;
  if(lastTs == null) lastTs = ts;
  const dt = (ts - lastTs)/1000; lastTs = ts;
  const perSec = +document.getElementById("speed").value;   // sim-minutes per real second
  let t = clock + dt*perSec;
  if(t >= TMAX){ t = TMAX; setPlaying(false); }
  // Auto-advance the decision plan as the clock passes each tick's time
  let newIdx = planIdx;
  for(let i = newIdx + 1; i < TRACE.ticks.length; i++){
    if(TRACE.ticks[i].time <= t) newIdx = i;
    else break;
  }
  if(newIdx !== planIdx){ planIdx = newIdx; renderPlan(); }
  updateClock(t);
  if(playing) raf = requestAnimationFrame(step);
}
function setPlaying(on){
  playing = on; lastTs = null;
  document.getElementById("play").innerHTML = on ? "&#10073;&#10073; Pause" : "&#9654; Play";
  if(on){ if(clock >= TMAX) updateClock(TMIN); raf = requestAnimationFrame(step); }
  else if(raf){ cancelAnimationFrame(raf); raf = null; }
}

function setTick(i){ planIdx = Math.max(0, Math.min(TRACE.ticks.length-1, i)); renderPlan(); }

function setDecPlay(on){
  const btn = document.getElementById("playDec");
  if(decTimer){ clearInterval(decTimer); decTimer = null; }
  if(on){
    if(planIdx >= TRACE.ticks.length-1) setTick(0);
    decTimer = setInterval(() => {
      if(planIdx >= TRACE.ticks.length-1) setDecPlay(false);
      else setTick(planIdx+1);
    }, 1400);
    btn.innerHTML = "&#10073;&#10073; Decisions";
  } else {
    btn.innerHTML = "&#9654; Decisions";
  }
}

document.getElementById("play").onclick = () => setPlaying(!playing);
document.getElementById("reset").onclick = () => { setPlaying(false); updateClock(TMIN); };
document.getElementById("scrub").oninput = (e) => { setPlaying(false); updateClock(TMIN + (e.target.value/1000)*SPAN); };
document.getElementById("tprev").onclick = () => { setDecPlay(false); setTick(planIdx-1); };
document.getElementById("tnext").onclick = () => { setDecPlay(false); setTick(planIdx+1); };
document.getElementById("playDec").onclick = () => setDecPlay(!decTimer);

// ---- server mode: scenario picker + live Run --------------------------------
function showError(msg){
  const box = document.getElementById("errorBox");
  if(!msg){ box.style.display = "none"; box.textContent = ""; return; }
  box.style.display = "block"; box.textContent = msg;
}

function runNow(){
  const scenario = document.getElementById("scenarioSelect").value;
  const solution = "solution_ilp";
  const btn = document.getElementById("runBtn"), status = document.getElementById("runStatus");
  btn.disabled = true; status.textContent = "running…"; showError(null);
  fetch(`/api/run?scenario=${encodeURIComponent(scenario)}&solution=${encodeURIComponent(solution)}`)
    .then(r => r.json())
    .then(data => {
      if(data.error){ showError(data.error); document.getElementById("chartPlaceholder").style.display=""; return; }
      const ph = document.getElementById("chartPlaceholder"); if(ph) ph.remove();
      loadTrace(data.trace);
    })
    .catch(err => showError(String(err)))
    .finally(() => { btn.disabled = false; status.textContent = ""; });
}

if(SERVER_MODE){
  document.getElementById("runbar").style.display = "flex";
  document.getElementById("runBtn").onclick = runNow;
  fetch("/api/scenarios").then(r => r.json()).then(data => {
    const sel = document.getElementById("scenarioSelect");
    sel.innerHTML = data.scenarios.map(s => `<option value="${s}">${s}</option>`).join("");
    if(data.scenarios.length) runNow();
  }).catch(err => showError("Could not load scenario list: " + err));
} else {
  document.getElementById("chartPlaceholder").remove();
  loadTrace(MODE.trace);
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
