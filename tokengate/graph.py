"""Code graph ("graphify"): files, symbols and import edges, stored as JSON.

Cursor reads compact outlines and neighbourhoods from this graph instead of
opening whole files, which is where most agent tokens go.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

GRAPH_VERSION = 1

TS_IMPORT = re.compile(
    r"""(?:^|\n)\s*(?:import|export)\s+(?:type\s+)?(?P<what>[\s\S]*?)\s+from\s+['"](?P<src>[^'"]+)['"]"""
    r"""|(?:^|\n)\s*import\s+['"](?P<side>[^'"]+)['"]"""
    r"""|require\(\s*['"](?P<req>[^'"]+)['"]\s*\)"""
    r"""|import\(\s*['"](?P<dyn>[^'"]+)['"]\s*\)"""
)
TS_DECL = re.compile(
    r"^(?P<export>export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?"
    r"(?P<kind>function\*?|class|interface|type|enum|const|let|var|abstract\s+class)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)",
    re.M,
)
SQL_DECL = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?(?P<kind>TABLE|VIEW|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[\w.]+)",
    re.I,
)
RESOLVE_EXT = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts", ".py"]


@dataclass
class Symbol:
    name: str
    kind: str
    line: int
    exported: bool
    signature: str


@dataclass
class FileNode:
    path: str
    lang: str
    lines: int
    tokens: int  # rough estimate (chars / 4)
    sha: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)       # resolved project-relative paths
    external: list[str] = field(default_factory=list)      # npm / pip packages
    imported_names: dict[str, list[str]] = field(default_factory=dict)  # path -> names


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _lang(path: Path) -> str:
    return {".py": "python", ".sql": "sql"}.get(path.suffix, "ts")


def _strip_json_comments(text: str) -> str:
    """JSONC -> JSON. String-aware, so "@/*" path patterns survive."""
    tokens = re.finditer(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/', text)
    out, last = [], 0
    for m in tokens:
        out.append(text[last:m.start()])
        out.append(m.group(0) if m.group(0).startswith('"') else "")
        last = m.end()
    out.append(text[last:])
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class Graph:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.root
        self.files: dict[str, FileNode] = {}
        self._aliases = self._load_aliases()

    # ------------------------------------------------------------------ build
    def _load_aliases(self) -> list[tuple[str, list[Path]]]:
        """tsconfig `paths` (e.g. "@/*" -> ["src/*"]) for every tsconfig in the tree."""
        out: list[tuple[str, list[Path]]] = []
        for ts in self._walk(names={"tsconfig.json", "jsconfig.json"}):
            try:
                opts = json.loads(_strip_json_comments(ts.read_text("utf-8"))).get("compilerOptions", {})
            except (ValueError, OSError):
                continue
            base = ts.parent / opts.get("baseUrl", ".")
            for pat, targets in (opts.get("paths") or {}).items():
                out.append((pat.rstrip("*"), [base / t.rstrip("*") for t in targets]))
        return out

    def _walk(self, names: set[str] | None = None):
        ignore = set(self.cfg.ignore)
        stack = [self.root]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.name in ignore or (e.name.startswith(".") and e.is_dir()):
                    continue
                if e.is_dir():
                    stack.append(e)
                elif names is not None:
                    if e.name in names:
                        yield e
                elif e.suffix in self.cfg.extensions:
                    yield e

    def build(self, incremental: bool = True) -> dict:
        old = self._load_raw() if incremental else {}
        old_files = old.get("files", {}) if old.get("version") == GRAPH_VERSION else {}
        seen, reused = {}, 0
        for path in self._walk():
            rel = path.relative_to(self.root).as_posix()
            try:
                text = path.read_text("utf-8", errors="replace")
            except OSError:
                continue
            sha = hashlib.sha1(text.encode()).hexdigest()[:12]
            prev = old_files.get(rel)
            if prev and prev["sha"] == sha:
                prev["symbols"] = [Symbol(**s) for s in prev["symbols"]]
                seen[rel] = FileNode(**prev)
                reused += 1
                continue
            seen[rel] = self._parse(path, rel, text, sha)
        self.files = seen
        self.save()
        return {"files": len(seen), "reparsed": len(seen) - reused, "edges": sum(len(f.imports) for f in seen.values())}

    def _parse(self, path: Path, rel: str, text: str, sha: str) -> FileNode:
        lang = _lang(path)
        node = FileNode(path=rel, lang=lang, lines=text.count("\n") + 1, tokens=estimate_tokens(text), sha=sha)
        if lang == "python":
            self._parse_py(path, node, text)
        elif lang == "sql":
            for m in SQL_DECL.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                node.symbols.append(Symbol(m["name"], m["kind"].lower(), line, True, m.group(0)[:160]))
        else:
            self._parse_ts(path, node, text)
        return node

    def _parse_ts(self, path: Path, node: FileNode, text: str) -> None:
        lines = text.splitlines()
        for m in TS_DECL.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            sig = lines[line_no - 1].strip()
            if len(sig) > 160:
                sig = sig[:157] + "..."
            kind = m["kind"].replace("abstract ", "")
            node.symbols.append(Symbol(m["name"], kind, line_no, bool(m["export"]), sig))
        for m in TS_IMPORT.finditer(text):
            spec = m["src"] or m["side"] or m["req"] or m["dyn"]
            names = re.findall(r"[A-Za-z_$][\w$]*", re.sub(r"\btype\b|\bas\s+\w+", "", m["what"] or ""))
            self._add_import(path, node, spec, names)

    def _parse_py(self, path: Path, node: FileNode, text: str) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        lines = text.splitlines()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(n, ast.ClassDef) else "function"
                sig = lines[n.lineno - 1].strip()
                node.symbols.append(Symbol(n.name, kind, n.lineno, not n.name.startswith("_"), sig[:160]))
                if isinstance(n, ast.ClassDef):
                    for c in n.body:
                        if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and not c.name.startswith("__"):
                            s = lines[c.lineno - 1].strip()
                            node.symbols.append(Symbol(f"{n.name}.{c.name}", "method", c.lineno, False, s[:160]))
            elif isinstance(n, ast.Import):
                for a in n.names:
                    self._add_import(path, node, a.name, [], py=True)
            elif isinstance(n, ast.ImportFrom):
                spec = "." * n.level + (n.module or "")
                self._add_import(path, node, spec, [a.name for a in n.names], py=True)

    def _add_import(self, path: Path, node: FileNode, spec: str, names: list[str], py: bool = False) -> None:
        target = self._resolve_py(path, spec) if py else self._resolve_ts(path, spec)
        if target:
            if target not in node.imports:
                node.imports.append(target)
            node.imported_names.setdefault(target, []).extend(n for n in names if n not in node.imported_names.get(target, []))
        else:
            pkg = spec.split(".")[0] if py else "/".join(spec.split("/")[: 2 if spec.startswith("@") else 1])
            if pkg and not spec.startswith(".") and pkg not in node.external:
                node.external.append(pkg)

    def _try_file(self, base: Path) -> str | None:
        cands = [base] if base.suffix and base.is_file() else []
        cands += [base.with_name(base.name + ext) for ext in RESOLVE_EXT]
        cands += [base / f"index{ext}" for ext in RESOLVE_EXT] + [base / "__init__.py"]
        for c in cands:
            if c.is_file():
                try:
                    return c.resolve().relative_to(self.root.resolve()).as_posix()
                except ValueError:
                    return None
        return None

    def _resolve_ts(self, path: Path, spec: str) -> str | None:
        if spec.startswith("."):
            return self._try_file(path.parent / spec)
        for prefix, targets in self._aliases:
            if prefix and spec.startswith(prefix):
                for t in targets:
                    hit = self._try_file(t / spec[len(prefix):])
                    if hit:
                        return hit
        return None

    def _resolve_py(self, path: Path, spec: str) -> str | None:
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            base = path.parent
            for _ in range(level - 1):
                base = base.parent
            rest = spec.lstrip(".")
            return self._try_file(base / rest.replace(".", "/")) if rest else self._try_file(base)
        return self._try_file(self.root / spec.replace(".", "/"))

    # ------------------------------------------------------------- persistence
    @property
    def path(self) -> Path:
        return self.cfg.state_dir / "graph.json"

    def _load_raw(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        data = {"version": GRAPH_VERSION, "files": {k: asdict(v) for k, v in self.files.items()}}
        self.path.write_text(json.dumps(data, separators=(",", ":")), "utf-8")

    def load(self, build_if_missing: bool = True) -> "Graph":
        raw = self._load_raw()
        if raw.get("version") != GRAPH_VERSION:
            if build_if_missing:
                self.build(incremental=False)
            return self
        for k, v in raw["files"].items():
            v["symbols"] = [Symbol(**s) for s in v["symbols"]]
            self.files[k] = FileNode(**v)
        return self

    # ----------------------------------------------------------------- queries
    def reverse(self) -> dict[str, list[str]]:
        rev: dict[str, list[str]] = defaultdict(list)
        for f in self.files.values():
            for t in f.imports:
                rev[t].append(f.path)
        return rev

    def find_symbol(self, name: str) -> list[dict]:
        q = name.lower()
        hits = []
        for f in self.files.values():
            for s in f.symbols:
                if s.name.lower() == q or (len(q) > 2 and q in s.name.lower()):
                    hits.append({"file": f.path, "line": s.line, "kind": s.kind, "exported": s.exported,
                                 "signature": s.signature, "exact": s.name.lower() == q})
        hits.sort(key=lambda h: (not h["exact"], not h["exported"], h["file"]))
        return hits

    def resolve_file(self, ref: str) -> str | None:
        ref = ref.replace("\\", "/").lstrip("./")
        if ref in self.files:
            return ref
        matches = [p for p in self.files if p.endswith(ref) or Path(p).stem == ref]
        return min(matches, key=len) if matches else None

    def outline(self, path: str) -> str:
        f = self.files[path]
        rows = [f"## {f.path}  ({f.lines} lines, ~{f.tokens} tok)"]
        if f.imports:
            rows.append("imports: " + ", ".join(f.imports))
        if f.external:
            rows.append("packages: " + ", ".join(f.external))
        for s in f.symbols:
            if s.kind in ("const", "let", "var") and not s.exported:
                continue  # skip local variables, keep outlines short
            rows.append(f"  L{s.line} {s.signature}")
        return "\n".join(rows)

    def neighbors(self, path: str, depth: int = 1) -> dict:
        rev = self.reverse()
        out = {"file": path, "imports": [], "imported_by": []}
        for key, edges in (("imports", lambda p: self.files[p].imports if p in self.files else []),
                           ("imported_by", lambda p: rev.get(p, []))):
            seen, frontier = {path}, [path]
            for _ in range(depth):
                nxt = []
                for p in frontier:
                    for q in edges(p):
                        if q not in seen:
                            seen.add(q)
                            nxt.append(q)
                out[key].extend(nxt)
                frontier = nxt
        return out

    def impact(self, path: str) -> list[str]:
        """Everything that transitively depends on `path` (what a change could break)."""
        rev = self.reverse()
        seen, q = set(), deque([path])
        while q:
            for d in rev.get(q.popleft(), []):
                if d not in seen:
                    seen.add(d)
                    q.append(d)
        return sorted(seen)

    def rank(self, query: str, limit: int = 8) -> list[tuple[str, float]]:
        """Cheap relevance ranking of files for a natural-language task."""
        words = [w for w in re.findall(r"[a-z0-9]+", _split_camel(query).lower()) if len(w) > 2]
        if not words:
            return []
        scores: dict[str, float] = defaultdict(float)
        for f in self.files.values():
            hay_path = _split_camel(f.path).lower()
            hay_sym = " ".join(_split_camel(s.name).lower() for s in f.symbols)
            for w in words:
                scores[f.path] += 3 * hay_path.count(w) + hay_sym.count(w)
        top = [p for p, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s > 0][:limit]
        # Give a little credit to direct neighbours of strong hits.
        for p in top[:3]:
            for n in self.files[p].imports:
                scores[n] += 0.5
        ranked = sorted(((p, s) for p, s in scores.items() if s > 0), key=lambda kv: -kv[1])
        return ranked[:limit]

    def overview(self, max_files: int = 400) -> str:
        """Compact repo map: one line per file with exported symbols."""
        by_dir: dict[str, list[FileNode]] = defaultdict(list)
        for f in self.files.values():
            by_dir[str(Path(f.path).parent)].append(f)
        rows = [f"# Repo map: {len(self.files)} files, ~{sum(f.tokens for f in self.files.values())} tokens total"]
        count = 0
        for d in sorted(by_dir):
            rows.append(f"{d}/")
            for f in sorted(by_dir[d], key=lambda x: x.path):
                exp = [s.name for s in f.symbols if s.exported][:8]
                rows.append(f"  {Path(f.path).name} ~{f.tokens}t" + (f": {', '.join(exp)}" if exp else ""))
                count += 1
                if count >= max_files:
                    rows.append("  ... (truncated)")
                    return "\n".join(rows)
        return "\n".join(rows)


def _split_camel(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s).replace("_", " ").replace("/", " ").replace(".", " ")
