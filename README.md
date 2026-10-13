# tokengate

**Free compute first, paid Cursor tokens last.** tokengate sits in front of Cursor. It answers your chat message with a code graph, a cache, a local model, or free cloud APIs (NVIDIA, Groq, GitHub Models, OpenRouter…). Cursor's own model only runs when all of those fail. A fuel gauge shows how much Cursor usage you have left and when you'll run out.

![gauge](docs/gauge-light.png)

## How Cursor is kept from going first

Cursor's chat model normally reads every message *before* it decides to call a tool, so an MCP server alone can't stop that first spend. tokengate adds a **front door** using Cursor's `beforeSubmitPrompt` hook (`.cursor/hooks.json`):

```
your message ──► tokengate hook ──► free model did it?  ── yes ──► prompt blocked, Cursor model never runs (0 tokens)
                                    │                                answer in popup + .tokengate/last_answer.md
                                    │                                edits already on disk (git diff)
                                    └─ no / not suitable ──► Cursor runs; .tokengate/HANDOFF.md has the context
```

- **`prompt_gate = "auto"`** (default) intercepts self-contained tasks: *explain, summarize, review, write tests, add docs*, and small edits in files you name. It passes through anything that depends on chat history ("yes do it", "fix the error above") or is open-ended.
- **`"always"`** intercepts every message. **`"off"`** disables the hook.
- When the fuel gauge is in **reserve**, `auto` behaves like `always`.
- Start a message with **`!`** to send it straight to Cursor.
- The hook **fails open**: if it errors, your message goes to Cursor as usual.

Inside Cursor's turn, the MCP tools and an `alwaysApply` rule keep it cheap: graph outlines instead of whole files, `delegate` for writing, and `run_checks` for verifying.

## The escalation ladder

| Tier | What | Cost |
|---|---|---|
| 0 | Code graph (outlines, symbols, imports, blast radius), ripgrep, local `tsc`/`eslint`/`ruff`/`pytest` | free |
| 1 | Cache: same task on the same code gives the same answer | free |
| 2 | Local model via Ollama | free |
| 3 | Free cloud APIs, in `tokengate.toml` order, each with its own per-minute and daily caps | free quota |
| 4 | Cursor, handed a minimal context pack | paid, last |

Free models can refuse with `ESCALATE: reason`. An edit is all-or-nothing (every `SEARCH` block must match exactly), and it is rolled back if typecheck or lint errors go up. Each failure moves on to the next model.

## Free API providers (pre-configured)

Add a key for any of these and it joins the ladder. A provider with no key is skipped. The limits below are as reported in October 2026. Free tiers change often, so tokengate's defaults sit slightly under them. Check each console.

| Provider | Sign up | Env var | Free limits (approx.) | Notes |
|---|---|---|---|---|
| Ollama (local) | ollama.com | – | unlimited | private; `ollama pull qwen2.5-coder:7b` |
| NVIDIA NIM | build.nvidia.com | `NVIDIA_API_KEY` | ~40 RPM | 100+ models; dev/test use only |
| Groq | console.groq.com | `GROQ_API_KEY` | ~30 RPM, ~1,000 req/day | very fast; daily token caps |
| Cerebras | cloud.cerebras.ai | `CEREBRAS_API_KEY` | reported inconsistently | defaults kept low |
| GitHub Models | github.com/marketplace/models | `GITHUB_MODELS_TOKEN` | ~15 RPM / 150 per day (Copilot Free) | PAT with `models:read`; more with paid Copilot |
| OpenRouter | openrouter.ai | `OPENROUTER_API_KEY` | 20 RPM, 50/day (1,000/day after a one-time $10) | many `:free` models |
| Mistral (Experiment) | console.mistral.ai | `MISTRAL_API_KEY` | low RPM, large monthly tokens | Codestral; **may train on prompts** |
| Google Gemini | aistudio.google.com | `GEMINI_API_KEY` | shown only in AI Studio | Flash/Flash-Lite; **may train on prompts** |

Providers that may train on your prompts (and therefore your code) are **off** unless you set `allow_training_providers = true`. Run `tokengate models` to list the real model IDs each key can use, then put the best ones first. To track changes to free tiers, see the community list [cheahjs/free-llm-api-resources](https://github.com/cheahjs/free-llm-api-resources).

## Fuel gauge

```bash
tokengate gauge                      # E [████████······················] F  28% left  … runs out 2026-10-19
tokengate gauge --set 72 --cycle-end 2026-10-28   # manual reading from cursor.com/dashboard
tokengate dashboard                  # http://127.0.0.1:8765 (gauge, pace vs cycle, free-provider caps, savings)
tokengate install-statusbar          # ▮▮▯▯▯ 28% in Cursor's status bar, turns red in reserve
```

- **Cursor usage** is read the way community trackers do it (MIT-licensed [cursor-usage-tracker](https://github.com/Tendo33/cursor-usage-tracker)): Cursor's own login token from its local `state.vscdb`, sent read-only to the `cursor.com/api/usage-summary` endpoint the dashboard uses. **This endpoint is unofficial and can change.** If auto-read fails, set `CURSOR_SESSION_TOKEN` (the `WorkosCursorSessionToken` cookie from your browser) or use `--set`. Readings are cached for 5 minutes.
- **Forecast**: a linear burn rate over the billing cycle, giving "projected % at cycle end" and "runs out on".
- **Levels**: `ok` → `warn` (≥ 60%, or the pace overshoots) → `reserve` (≥ 85%, or the pace is far over) → `empty`. Thresholds live in `tokengate.toml`. From `warn` upward, every tokengate tool output starts with a `[gauge …]` line telling Cursor's agent to economise.
- **Free providers**: none of them expose remaining quota, so the gauge shows tokengate's own usage against each provider's caps.

## Install

```bash
pip install "git+https://github.com/<you>/tokengate.git#egg=tokengate[mcp]"   # or: pip install -e ./tokengate[mcp]
cd your-project
tokengate init      # tokengate.toml, .cursor/{mcp.json,hooks.json,rules/tokengate.mdc}, .cursorignore, graph
tokengate models    # check model IDs per provider
```

Set the API keys you have as environment variables. On Windows: `setx NVIDIA_API_KEY "nvapi-..."`. Then restart Cursor and turn on **tokengate** under *Settings → MCP*. Run `tokengate init` on each machine, because it writes that machine's Python path into the Cursor configs.

## MCP tools

`repo_map`, `graph_file`, `graph_symbol`, `graph_impact`, `context_pack`, `search`, `run_checks`, `delegate`, `distribute`, `savings`, `fuel_gauge`. Each tool's description explains when to use it.

## CLI

```bash
tokengate viz                     # .tokengate/graph.html (interactive), graph.mmd, REPO_MAP.md
tokengate query symbol detectMisquote | file ReaderScreen | impact src/constants/bible.constants.ts
tokengate ask "explain the search pipeline" -k explain
tokengate ask "add input validation to parseReference" -f Layer1_ReferenceParser.ts -k edit
tokengate fanout "list bugs and a11y issues" "src/screens/**/*.tsx" -k review
tokengate check | stats | gauge | dashboard
```

## Prior art (what to borrow next)

| Project | What it does well | How tokengate relates |
|---|---|---|
| [graphify](https://graphify.com/integrations/cursor) (PyPI `graphifyy`, MIT) | tree-sitter AST graph, MCP server, `graphify cursor install`, ~7–50x fewer tokens in independent tests | same idea as tokengate's graph tier. Swap in its graph for more languages and call-level edges |
| aider repo map | tree-sitter tags + PageRank, budget-fitted map | tokengate's `rank()` is a lightweight version; PageRank is the upgrade path |
| [Local Worker MCP](https://glama.ai/mcp/servers/xcj1rfla17), [Claude Code Token Saver](https://mcp.so/servers/cc_token_saver_mcp), [Ultimate MCP / llm_gateway](https://glama.ai/mcp/servers/Dicklesworthstone/llm_gateway_mcp_server) | delegate mechanical work to cheap or local models | tokengate adds edit validation, rollback, per-provider caps and the prompt-gate hook |
| [cursor-usage-tracker](https://github.com/Tendo33/cursor-usage-tracker) (MIT), cursor-stats (archived) | Cursor usage in the status bar | the gauge reuses their documented approach (no code copied) |

## Limits

- `beforeSubmitPrompt` can block, but not rewrite, a prompt. Forum reports mention a double popup and occasional non-blocking in some Cursor versions, so test it on yours. Cursor's hook timeout applies: very slow free models can let the prompt through.
- Cursor's agent still chooses whether to call MCP tools. The rule strongly encourages it but can't force it.
- Savings figures are estimates (characters / 4).
- Delegated edits come from smaller models. Review the `git diff`.

## Tests

```bash
pip install pytest && pytest -q
```
