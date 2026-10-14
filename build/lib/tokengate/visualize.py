"""Graph exports: interactive HTML, Mermaid, and a compact REPO_MAP.md for Cursor."""

from __future__ import annotations

import json
from pathlib import Path

from .graph import Graph

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Code Graph</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
 :root{--bg:#fafafa;--fg:#222;--panel:#fff;--line:#ddd}
 @media (prefers-color-scheme:dark){:root{--bg:#16181d;--fg:#e6e6e6;--panel:#1f2229;--line:#333}}
 body{margin:0;font:13px system-ui,sans-serif;background:var(--bg);color:var(--fg);display:flex;height:100vh}
 #net{flex:1} #side{width:340px;padding:12px;overflow:auto;background:var(--panel);border-left:1px solid var(--line)}
 input{width:100%;padding:6px;box-sizing:border-box;margin-bottom:8px} pre{white-space:pre-wrap;font-size:12px}
 @media (max-width:700px){body{flex-direction:column}#side{width:auto;height:40vh}}
</style></head><body>
<div id="net"></div>
<div id="side"><input id="q" placeholder="filter files / symbols…"><div id="info">Click a node. Size = file tokens.</div></div>
<script>
const DATA = __DATA__;
const palette = ["#4e79a7","#f28e2b","#e15759","#76b7b2","#59a14f","#edc948","#b07aa1","#ff9da7","#9c755f","#bab0ac"];
const groups = [...new Set(DATA.nodes.map(n => n.group))];
const nodes = new vis.DataSet(DATA.nodes.map(n => ({id:n.id, label:n.label, value:n.tokens,
  color:palette[groups.indexOf(n.group)%palette.length], title:n.id})));
const edges = new vis.DataSet(DATA.edges.map(([a,b]) => ({from:a,to:b,arrows:"to"})));
const net = new vis.Network(document.getElementById("net"), {nodes, edges},
  {nodes:{shape:"dot",scaling:{min:6,max:40},font:{color:getComputedStyle(document.body).color}},
   edges:{color:{opacity:.35},smooth:false}, physics:{stabilization:{iterations:200}}});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[c]);
const byId = Object.fromEntries(DATA.nodes.map(n => [n.id, n]));
net.on("click", e => { if(!e.nodes.length) return; const n = byId[e.nodes[0]];
  const imp = DATA.edges.filter(x=>x[0]===n.id).map(x=>x[1]), by = DATA.edges.filter(x=>x[1]===n.id).map(x=>x[0]);
  document.getElementById("info").innerHTML = `<b>${esc(n.id)}</b><br>~${n.tokens} tokens<pre>${esc(n.outline)}</pre>
  <b>imports</b><pre>${esc(imp.join("\\n")||"-")}</pre><b>imported by</b><pre>${esc(by.join("\\n")||"-")}</pre>`; });
document.getElementById("q").oninput = e => { const q = e.target.value.toLowerCase();
  nodes.update(DATA.nodes.map(n => ({id:n.id, hidden: !!q && !(n.id.toLowerCase().includes(q) || n.outline.toLowerCase().includes(q))}))); };
</script></body></html>"""


def export(graph: Graph) -> dict[str, Path]:
    out = graph.cfg.state_dir
    nodes = [{"id": f.path, "label": Path(f.path).name, "tokens": f.tokens,
              "group": f.path.split("/")[1] if f.path.startswith("src/") and f.path.count("/") > 1 else f.path.split("/")[0],
              "outline": "\n".join(f"L{s.line} {s.signature}" for s in f.symbols if s.exported)[:2000]}
             for f in graph.files.values()]
    edges = [[f.path, t] for f in graph.files.values() for t in f.imports]

    html = out / "graph.html"
    html.write_text(HTML.replace("__DATA__", json.dumps({"nodes": nodes, "edges": edges}).replace("</", "<\\/")), "utf-8")

    ids = {p: f"n{i}" for i, p in enumerate(graph.files)}
    mmd = ["graph LR"] + [f'  {ids[p]}["{p}"]' for p in graph.files] + [f"  {ids[a]} --> {ids[b]}" for a, b in edges]
    mermaid = out / "graph.mmd"
    mermaid.write_text("\n".join(mmd), "utf-8")

    repo_map = out / "REPO_MAP.md"
    repo_map.write_text(graph.overview(), "utf-8")
    return {"html": html, "mermaid": mermaid, "repo_map": repo_map}
