# tokengate

A gate between Cursor and your code. Before Cursor spends paid tokens, tokengate tries free options in this order:

| Tier | What | Cost |
|---|---|---|
| 0 | **Code graph** (outlines, symbols, imports, blast radius), ripgrep search, local `tsc` / `eslint` / `ruff` / `pytest` | free |
| 1 | **Cache** of earlier answers (same task + same code gives the same answer) | free |
| 2 | **Local model** via Ollama, if it's running | free |
| 3 | **NVIDIA NIM** (build.nvidia.com free credits); several models, rate-limited, run in parallel | free quota |
| 4 | **Cursor**, handed a minimal context pack so it doesn't re-explore | paid, last |

Cursor talks to it over **MCP**. A Cursor rule (`.cursor/rules/tokengate.mdc`, `alwaysApply`) tells the agent to use these tools first.

## Why this saves tokens

Most agent tokens go to *reading* code (opening whole files to find one function) and *writing* long outputs.

- **Reading**: `graph_file` returns a ~100-token outline (signatures plus line numbers) instead of a 1,000+ token file. `repo_map` replaces exploratory `ls` and `read` loops.
- **Writing**: `delegate` makes a free model produce the edit and writes it to disk. Cursor only sees `status=done, files changed: …`.
- **Batch work** (`distribute`) never touches Cursor at all. Results go to `.tokengate/out/`.
- **`.cursorignore`** keeps `node_modules`, `android/`, `ios/` and lockfiles out of Cursor's index and @-context.

## Install (once)

```bash
pip install -e path/to/tokengate[mcp]        # Python 3.10+
```

Get a free key at https://build.nvidia.com (open any model, then "Get API Key") and set it:

```powershell
# Windows (PowerShell), persistent
setx NVIDIA_API_KEY "nvapi-..."
```
```bash
# macOS / Linux
echo 'export NVIDIA_API_KEY=nvapi-...' >> ~/.zshrc
```

Optional local tier: install [Ollama](https://ollama.com), then run `ollama pull qwen2.5-coder:7b`.

## Plug into a project

```bash
cd your-project
tokengate init        # writes tokengate.toml, .cursor/mcp.json, .cursor/rules/tokengate.mdc,
                      # .cursorignore, adds .tokengate/ to .gitignore, builds the graph
tokengate models      # see which NVIDIA models your key can use; put the best first in tokengate.toml
```

Restart Cursor, then turn on **tokengate** under *Settings → MCP*. Run `tokengate init` again on each machine; it writes that machine's Python path into `.cursor/mcp.json`.

## MCP tools (what Cursor sees)

| Tool | Use |
|---|---|
| `repo_map` | whole-repo map: files, exported symbols, token sizes |
| `graph_file(path)` | outline of a file plus imports and imported-by |
| `graph_symbol(name)` | where something is defined, with its signature |
| `graph_impact(path)` | everything that transitively depends on a file |
| `context_pack(task)` | smallest relevant context for a task |
| `search(regex)` | capped ripgrep |
| `run_checks()` | typecheck, lint and tests; returns only failures |
| `delegate(task, files, kind)` | free models do the work. `kind`: `edit`, `tests`, `docs`, `review`, `explain`, `summarize`, `general` |
| `distribute(task, glob, kind)` | same task over many files, spread across all free models |
| `savings()` | estimated Cursor tokens avoided per tier |

### How `delegate` decides it's good enough

1. The model can refuse with `ESCALATE: reason`. tokengate then moves on to the next model.
2. Edits must come back as `SEARCH/REPLACE` blocks. **All** blocks must match the file exactly, otherwise nothing is written and the next model is tried.
3. With `verify=True`, tokengate counts typecheck and lint errors before and after the edit. If the count goes up, the edit is rolled back and the next model is tried.
4. Only when every free option fails does it return `status=escalate`, together with a context pack for Cursor.

## CLI (same features, no Cursor needed)

```bash
tokengate viz                                   # .tokengate/graph.html (interactive), graph.mmd, REPO_MAP.md
tokengate query symbol detectMisquote
tokengate query impact src/constants/bible.constants.ts
tokengate ask "add input validation to parseReference" -f Layer1_ReferenceParser.ts -k edit
tokengate ask "explain the search pipeline" -k explain
tokengate fanout "list bugs and accessibility issues" "src/screens/**/*.tsx" -k review
tokengate check
tokengate stats
```

## Config: `tokengate.toml`

Providers are tried top to bottom. You can add any OpenAI-compatible endpoint (Groq, OpenRouter free models, LM Studio, and so on) as another `[[providers]]` block with `name`, `base_url`, `api_key_env`, `models`, `rpm` and `max_parallel`.

## Languages

TypeScript/JavaScript (including tsconfig `paths` aliases like `@/`), Python (AST-based), and SQL (`CREATE TABLE`, `VIEW`, `INDEX`, `TRIGGER`). There are no runtime dependencies besides `mcp` for the server.

## Limits (be aware)

- Cursor's agent *chooses* whether to call tools. The rule makes it strongly prefer tokengate, but it can't force it. Check `savings()` and the `escalations_to_cursor` count.
- Token savings are estimates (characters / 4).
- Free NVIDIA credits and rate limits are set by NVIDIA. Lower `rpm` if you see 429 errors.
- Delegated code is written by smaller models. Review the `git diff`, as you would for any agent edit.

## Tests

```bash
pip install pytest && pytest -q
```
