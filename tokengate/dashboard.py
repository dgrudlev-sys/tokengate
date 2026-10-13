"""Local gas-gauge dashboard: `tokengate dashboard` -> http://127.0.0.1:8765"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gauge
from .cache import Store
from .config import Config

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token Gauge</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#677084;--line:#e3e6ec;--ok:#2e9e5b;--warn:#d99a00;--bad:#d64545;--free:#3d7be0}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121419;--card:#1b1e25;--fg:#e8eaef;--muted:#9aa3b5;--line:#2a2e38}}
:root[data-theme="dark"]{--bg:#121419;--card:#1b1e25;--fg:#e8eaef;--muted:#9aa3b5;--line:#2a2e38}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:20px 16px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em}
.hero{grid-column:1/-1;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.hero svg{width:300px;max-width:100%}
.big{font-size:34px;font-weight:700;font-variant-numeric:tabular-nums}
.msg{font-size:15px;max-width:440px}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:12px;color:#fff}
.track{height:10px;background:var(--line);border-radius:6px;position:relative;margin:6px 0 2px}
.fill{height:100%;border-radius:6px}
.tick{position:absolute;top:-4px;width:2px;height:18px;background:var(--fg)}
.row{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}
.kv{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--line)}.kv:last-child{border:0}
.kv b{font-variant-numeric:tabular-nums}
form{display:flex;gap:8px;margin-top:8px}input{flex:1;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{padding:6px 12px;border:0;border-radius:6px;background:var(--free);color:#fff;cursor:pointer}
.err{color:var(--bad);font-size:12px}
</style></head><body><main>
<h1>Token gauge</h1><p class="sub" id="sub">loading…</p>
<div class="grid">
  <div class="card hero">
    <svg viewBox="0 0 300 185" role="img" aria-label="Cursor fuel gauge">
      <path d="M30 150 A120 120 0 0 1 270 150" fill="none" stroke="var(--line)" stroke-width="22" stroke-linecap="round"/>
      <path id="arc" d="M30 150 A120 120 0 0 1 270 150" fill="none" stroke="var(--ok)" stroke-width="22" stroke-linecap="round" pathLength="100" stroke-dasharray="0 100"/>
      <line id="needle" x1="150" y1="150" x2="150" y2="48" stroke="var(--fg)" stroke-width="4" stroke-linecap="round" style="transform-origin:150px 150px;transition:transform .8s"/>
      <circle cx="150" cy="150" r="9" fill="var(--fg)"/>
      <text x="22" y="175" fill="var(--muted)" font-size="16" font-weight="700">E</text>
      <text x="268" y="175" fill="var(--muted)" font-size="16" font-weight="700">F</text>
      <text x="150" y="176" text-anchor="middle" fill="var(--muted)" font-size="11">CURSOR FUEL LEFT</text>
    </svg>
    <div style="flex:1;min-width:240px"><div class="big" id="left">–</div><span class="pill" id="level">–</span><p class="msg" id="msg"></p><div class="err" id="err"></div></div>
  </div>
  <div class="card"><h2>Pace this billing cycle</h2>
    <div>Usage</div><div class="track"><div class="fill" id="useBar"></div><div class="tick" id="timeTick"></div></div>
    <div class="row"><span id="useTxt"></span><span id="timeTxt"></span></div>
    <div class="kv" style="margin-top:10px"><span>Projected at cycle end</span><b id="proj">–</b></div>
    <div class="kv"><span>Runs out on</span><b id="out">–</b></div>
    <div class="kv"><span>Cycle ends</span><b id="end">–</b></div>
  </div>
  <div class="card"><h2>Free providers, last 24h</h2>
    <div id="free"></div>
    <div class="kv" style="margin-top:6px"><span>Free tokens used, 30d</span><b id="t30">–</b></div>
  </div>
  <div class="card"><h2>Cursor tokens saved, 30 days</h2>
    <div class="big" id="saved">–</div>
    <div id="tiers"></div>
  </div>
  <div class="card"><h2>Usage history</h2>
    <svg id="spark" viewBox="0 0 260 70" style="width:100%;height:70px"></svg>
    <form id="manual"><input id="pct" type="number" min="0" max="100" step="0.1" placeholder="% used from cursor.com/dashboard"><button>Set</button></form>
  </div>
</div></main>
<script>
const $ = id => document.getElementById(id);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const COLORS = {ok:"--ok", warn:"--warn", reserve:"--bad", empty:"--bad", unknown:"--muted"};
const fmt = n => n == null ? "–" : Intl.NumberFormat().format(Math.round(n));
async function load(refresh) {
  const r = await (await fetch("/api/gauge" + (refresh ? "?refresh=1" : ""))).json();
  const used = r.percent_used, left = used == null ? null : Math.max(0, 100 - used), col = css(COLORS[r.level]);
  $("arc").setAttribute("stroke-dasharray", `${left ?? 0} 100`); $("arc").setAttribute("stroke", col);
  $("needle").style.transform = `rotate(${((left ?? 50) / 100) * 180 - 90}deg)`;
  $("left").textContent = left == null ? "unknown" : `${left.toFixed(0)}% left`;
  $("level").textContent = r.level.toUpperCase(); $("level").style.background = col;
  $("msg").textContent = r.message; $("err").textContent = r.error ? (r.source === "manual" ? "Auto-read unavailable, showing your manual reading: " : "") + r.error : "";
  $("err").style.color = r.source === "manual" ? css("--muted") : css("--bad");
  $("sub").textContent = `${r.plan || "Cursor"} · source: ${r.source}` + (r.checked_at ? ` · checked ${new Date(r.checked_at).toLocaleString()}` : "")
    + (r.used != null ? ` · $${r.used.toFixed(2)} of $${r.limit.toFixed(2)}` : "");
  $("useBar").style.width = `${Math.min(100, used ?? 0)}%`; $("useBar").style.background = col;
  $("timeTick").style.left = `${r.cycle_elapsed_pct ?? 0}%`; $("timeTick").style.display = r.cycle_elapsed_pct == null ? "none" : "";
  $("useTxt").textContent = used == null ? "" : `${used.toFixed(0)}% used`;
  $("timeTxt").textContent = r.cycle_elapsed_pct == null ? "" : `▲ ${r.cycle_elapsed_pct.toFixed(0)}% of cycle elapsed`;
  $("proj").textContent = r.projected_pct_at_cycle_end == null ? "–" : `${r.projected_pct_at_cycle_end.toFixed(0)}%`;
  $("out").textContent = r.runs_out_on || "not at this pace";
  $("end").textContent = r.cycle_end ? new Date(r.cycle_end).toLocaleDateString() : "–";
  $("free").innerHTML = r.free.providers.map(p => {
    const cap = p.rpd_limit, pct = cap ? Math.min(100, 100 * p.requests_24h / cap) : 0;
    const state = !p.available ? (p.may_train ? "off (may train)" : "no key") : `${p.requests_24h}${cap ? " / " + cap : ""} req`;
    const c = pct >= 90 ? css("--bad") : pct >= 60 ? css("--warn") : css("--free");
    return `<div style="margin-bottom:8px;opacity:${p.available ? 1 : .55}"><div class="row" style="color:var(--fg);font-size:13px"><span>${p.name}</span><span>${state}</span></div>
      <div class="track" style="height:6px;margin:3px 0"><div class="fill" style="width:${cap ? pct : 0}%;background:${c}"></div></div></div>`;
  }).join("");
  $("t30").textContent = fmt(r.free.tokens_30d);
  $("saved").textContent = `~${fmt(r.savings.total_cursor_tokens_avoided)}`;
  $("tiers").innerHTML = Object.entries(r.savings.tiers).map(([k, v]) =>
    `<div class="kv"><span>${k}</span><b>${v.calls} calls · ~${fmt(v.cursor_tokens_avoided)}</b></div>`).join("");
  const h = r.history, s = $("spark");
  if (h.length > 1) {
    const t0 = h[0][0], t1 = h[h.length - 1][0] || t0 + 1;
    const pts = h.map(([t, p]) => `${(260 * (t - t0) / (t1 - t0 || 1)).toFixed(1)},${(66 - 0.62 * p).toFixed(1)}`).join(" ");
    s.innerHTML = `<line x1="0" x2="260" y1="4" y2="4" stroke="${css("--line")}"/><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2"/>`;
  } else s.innerHTML = `<text x="0" y="40" fill="${css("--muted")}" font-size="12">History appears after a few readings.</text>`;
}
$("manual").onsubmit = async e => { e.preventDefault(); await fetch("/api/set?percent=" + encodeURIComponent($("pct").value), {method: "POST"}); load(); };
load(true); setInterval(load, 30000);
</script></body></html>"""


def serve(cfg: Config, port: int = 8765) -> None:
    store = Store(cfg.state_dir, cfg.cache_ttl_days)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str, ctype: str) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/gauge":
                refresh = "refresh" in urllib.parse.parse_qs(url.query)
                self._send(200, gauge.to_json(gauge.read(cfg, store, refresh=refresh)), "application/json")
            elif url.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/set":
                try:
                    pct = float(urllib.parse.parse_qs(url.query)["percent"][0])
                except (KeyError, ValueError):
                    return self._send(400, json.dumps({"error": "percent required"}), "application/json")
                gauge.set_manual(store, max(0.0, min(100.0, pct)))
                return self._send(200, "{}", "application/json")
            self._send(404, "not found", "text/plain")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only
    print(f"Token gauge: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
