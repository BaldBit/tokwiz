# Product Scope Document: TokWiz (`tokwiz`)

| Metadata | Details |
| :--- | :--- |
| **Product Name** | **TokWiz** (`tokwiz`) |
| **Tagline** | *Think efficiently. Forecast ahead. Use fewer tokens.* |
| **Target Platforms** | Claude Code, OpenAI Codex CLI, Google Gemini CLI / Antigravity (plus Cursor & GitHub Copilot adapters) |
| **Core Architecture** | Portable Agent Skill (`SKILL.md`) + Tier 2 Tree-Sitter Project Graph + Lifecycle Hooks + Local Analytics Dashboard |
| **Status** | Draft v1.0 |
| **Last Updated** | 2026-11-14 |

---

## 1. Executive Summary & Vision

**TokWiz** is an intelligent, cross-agent token optimization layer, pre-prompt cost forecaster, and usage analytics platform built for AI coding agents and general-purpose AI assistants.

Unlike simple prompt-shortening instructions, **TokWiz** optimizes the entire agent execution loop—governing **what context the agent consumes**, **how it navigates codebases**, **how tool outputs are filtered**, and **how prompt caches are preserved**—while maintaining 95%+ reasoning and output quality.

### Core Project Goals
1. **Reduce Token Consumption:** Cut overall workflow token usage by **30%–65%** across multi-step tasks without sacrificing accuracy, safety, or business logic.
2. **Eliminate Token Wastage:** Prevent blind `grep`/`find` thrashing, unbounded full-file reads, lockfile ingestion, redundant tool calls, and noisy shell logs.
3. **Optimize Token Usage (Cache & Context Hygiene):** Maximize provider KV-cache hit rates (75%–90% cost discount) and isolate noisy exploration in forked subagents.
4. **Forecast Cost & Tokens Ahead of Execution:** Show the user an accurate, zero-LLM-cost prediction of required input, tool-loop, and output tokens—plus monetary cost (`$`)—the moment a prompt is entered.
5. **Visualize Savings via a Statistics Dashboard:** Provide a local CLI and interactive web dashboard tracking forecasted vs. actual tokens, net dollar savings, cache efficiency, and graph hit rates across Claude, Codex, and Gemini.

---

## 2. Target AI Agent Ecosystem

TokWiz uses a **portable core** (following the open `SKILL.md` standard) paired with lightweight **host-specific adapters** and **lifecycle hooks**:

| Agent / Host | Skill Discovery | Always-On Rule Bootstrapper | Pre-Prompt Forecast & Tool Guardrail Hooks |
| :--- | :--- | :--- | :--- |
| **Claude / Claude Code** | `.claude/skills/tokwiz/SKILL.md` | `CLAUDE.md` | `.claude/settings.json` (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`) |
| **OpenAI Codex CLI** | `.agents/skills/tokwiz/SKILL.md` | `AGENTS.md` | `tokwiz` CLI wrapper + Git/Shell hooks + MCP/script tools |
| **Gemini CLI / Antigravity** | `.agents/skills/tokwiz/SKILL.md` | `GEMINI.md` / `AGENTS.md` | `.agents/hooks.json` (`PreInvocation`, `PreToolUse`, `PostToolUse`, `Stop`) |
| **Cursor / GitHub Copilot** | `.agents/skills/tokwiz/SKILL.md` | `.cursorrules` / `.github/copilot-instructions.md` | CLI tasks & MCP server integration |

---

## 3. Core Functional Pillars

### Pillar 1: Tier 2 Project Graph Engine (Tree-Sitter + Personalized PageRank)

To eliminate expensive multi-turn `find` and `grep` discovery loops, TokWiz includes a local, zero-API-cost structural codebase graph (`scripts/project-graph.py`).

#### 3.1.1 How It Works
* **Multi-Language AST Parsing:** Uses `tree-sitter` (`tree-sitter-language-pack`) to deterministically parse source files across 40+ languages (TypeScript/JavaScript, Python, Go, Rust, Java, C/C++, Ruby, C#, etc.) with a zero-dependency Python AST/regex fallback if native bindings are unavailable.
* **Graph Schema (`.tokwiz/graph.db` in SQLite):**
  * **Nodes:** Files, Classes, Interfaces, Functions, Methods, and Type Definitions—each storing its exact `file_path`, `start_line`, `end_line`, byte range, and compact signature.
  * **Edges:**
    * `IMPORTS` (`File A` → `File B`)
    * `DEFINES` (`File A` → `Symbol X`)
    * `CALLS / REFERENCES` (`Symbol X` → `Symbol Y`)
    * `INHERITS / IMPLEMENTS` (`Class A` → `Interface B`)
* **Personalized PageRank Ranking:** When queried with prompt keywords or symbol names, TokWiz runs Personalized PageRank seeded on matched nodes to score the most relevant definitions, callers, and callees in the repository.
* **Incremental Indexing (`O(changed files)`):** Keyed by file `mtime`, byte size, and SHA-1 hash. Cold indexing 1,000+ files takes **~1.5 seconds**; incremental updates on edited files take **<40ms** at **$0 API cost**.

#### 3.1.2 Budget-Capped Subgraph Slicing (Preventing Graph Bloat)
* Never injects the full repository graph into the model context.
* Queries return a **1-hop or 2-hop neighborhood slice** strictly capped at a configurable token budget (default: `--max-tokens 400`):
  ```text
  [TokWiz Graph Slice: validateToken (340 tokens)]
  DEF src/auth/session.ts:L30-L52 fn validateToken(token: string, opts?: AuthOpts): Promise<Session>
  CALLERS (3):
    - src/middleware/requireAuth.ts:L12-L28
    - src/routes/login.ts:L44-L70
    - tests/auth.test.ts:L10-L45
  CALLEES (2):
    - src/crypto/jwt.ts:L15-L38 fn verifySignature()
    - src/db/users.ts:L88-L104 fn findUserById()
  ```
* Enables the agent to skip `grep`/`find` completely and perform surgical `read_file` calls using exact `start_line`–`end_line` coordinates.

---

### Pillar 2: Ahead-of-Time Token & Cost Forecaster

Before the agent consumes a single LLM token, TokWiz predicts the full workflow cost of the user's prompt (`scripts/token-estimator.py`).

#### 3.2.1 Multi-Step Workflow Prediction Formula
Single-turn character counting underestimates agentic workflows by 5x–20x. TokWiz predicts **Total Workflow Tokens** across the expected tool-execution loop:

$$\text{Total Forecast} = T_{\text{context}} + T_{\text{prompt}} + \sum_{i=1}^{N_{\text{steps}}} \left( T_{\text{context}, i} + T_{\text{tool\_in}, i} + T_{\text{tool\_out}, i} \right) + T_{\text{reasoning}} + T_{\text{output}}$$

1. **Prompt & Context Baseline ($T_{\text{context}} + T_{\text{prompt}}$):**
   * Counts current transcript history, system rules, `@file` attachments, and user prompt tokens using model-accurate tokenizers (`tiktoken` for OpenAI/Codex, calibrated BPE/character estimators or provider token-count APIs for Claude & Gemini).
2. **Graph-Assisted Blast-Radius Analysis:**
   * Scans the user prompt against the **Tier 2 Project Graph** in `<20ms`.
   * Identifies matched symbols, number of impacted files (1-hop/2-hop dependents), and exact line counts of relevant functions.
3. **Task Complexity Classifier:**
   * Classifies the prompt into one of five execution profiles:
     * **Profile A — Direct Q&A / Explanation:** `0–1` tool calls, Level 1 optimization.
     * **Profile B — Targeted Single-File Edit:** `2–4` tool calls, Level 1 optimization.
     * **Profile C — Multi-File Feature / Refactor:** `5–10` tool calls, Level 2 optimization + Graph Slice.
     * **Profile D — Deep Debugging / Test Triage:** `6–14` tool calls, Level 2/3 optimization + Output Filtering.
     * **Profile E — Repo-Wide Exploration / Architecture:** `8–18` tool calls, Level 2/3 + Subagent Isolation.
4. **Cross-Provider Cost & Cache Calculation:**
   * Computes projected cost (`$`) using an up-to-date pricing table (`references/pricing-matrix.json`) for:
     * **Claude:** Claude 4 / 3.7 Sonnet, Opus, Haiku (accounting for 90% prompt cache read discounts).
     * **OpenAI / Codex:** `codex-mini`, `o3`, `o4-mini`, `gpt-4.1` (accounting for cached inputs & reasoning token multipliers).
     * **Gemini:** Gemini 2.5 / 4 Pro & Flash (accounting for context caching tiers).

#### 3.2.2 Zero-Token Forecast UI Delivery
* **Gemini / Antigravity:** Injected via `PreInvocation` hook using `metadata.user_markdown` (rendered inline to the user while sending `""` to the model, costing **0 tokens**).
* **Claude Code:** Displayed via `UserPromptSubmit` hook output / status banner, with optional budget-gate confirmation if a prompt is predicted to exceed a user-defined cost threshold (e.g., `>$0.50`).
* **Codex CLI / Terminal:** Printed via `tokwiz predict "<prompt>"` or interactive wrapper before execution.

Example Forecast Banner:
> **⚡ TokWiz Forecast** | **Complexity:** Multi-File Refactor (Level 2) | **Blast Radius:** 4 files (`validateToken` + 3 callers, 162 lines)
> * **Estimated Workflow:** ~3,400 tokens *(vs. ~14,800 unoptimized — **77% Est. Savings**)*
> * **Est. Cost:** Claude Sonnet: **\$0.008** (Cached: \$0.003) · Codex (`o4-mini`): **\$0.004** · Gemini Pro: **\$0.004**

#### 3.2.3 Forecast vs. Real Cost Variance & Self-Calibration Engine
Predicting multi-step agentic workflows requires continuously measuring the gap between **Forecasted Cost** (before the turn starts) and **Real Cost** (after the turn completes):

1. **Pre/Post Turn Reconciliation (`forecast_id`):**
   * On prompt entry (`PreInvocation` / `UserPromptSubmit`), TokWiz logs a `forecast_id` with predicted input, cached input, tool-loop steps ($N_{\text{est}}$), output tokens, and forecasted cost ($C_{\text{forecast}}$).
   * On turn completion (`PostInvocation` / `Stop` via `hooks/post-turn-record.py`), TokWiz parses the host agent's transcript and usage metadata (Claude Code `~/.claude/projects/.../*.jsonl` usage blocks, Gemini/Antigravity `transcript.jsonl`, Codex CLI session logs) to extract **actual provider-billed tokens** and compute real cost ($C_{\text{real}}$).
2. **Variance & Drift Metrics:**
   * **Token Delta ($\Delta T$):** $\Delta T = T_{\text{real}} - T_{\text{forecast}}$ (broken down by Input, Cached Input, Tool Output, and Reasoning/Output).
   * **Cost Delta ($\Delta C$):** $\Delta C = C_{\text{real}} - C_{\text{forecast}}$ (in `$` USD and percentage error).
   * **MAPE (Mean Absolute Percentage Error):**
     $$\text{MAPE} = \frac{1}{M} \sum_{k=1}^{M} \left| \frac{C_{\text{real}, k} - C_{\text{forecast}, k}}{C_{\text{real}, k}} \right| \times 100\%$$
   * **Root-Cause Variance Attribution:** Automatically tags *why* a forecast drifted from reality:
     * `EXTRA_TOOL_LOOPS`: Agent ran more tool steps than $N_{\text{est}}$ (e.g., unexpected failing test retry loop).
     * `CACHE_MISS_DRIFT`: Expected a KV-cache hit (90% discount), but provider billed full uncached input rates (e.g., cache TTL expired).
     * `REASONING_SPIKE`: Model consumed significantly more internal thinking/reasoning tokens than baseline for that complexity profile.
     * `UNBOUNDED_TOOL_OUTPUT`: A shell command or MCP tool returned an unusually large payload.
3. **Adaptive Self-Calibration (`.tokwiz/calibration.json`):**
   * Uses an Exponential Moving Average (EMA) over recent turns in the repository to dynamically tune per-profile step multipliers ($N_{\text{est}}$), average tool output sizes, and model-specific reasoning ratios—steadily driving Forecast vs. Real Cost error inside the **±15%–20% target band**.

---

### Pillar 3: Ten Optimization Strategies & 3-Level Adaptive Engine

#### 3.3.1 The Ten Optimization Strategies

| # | Strategy | Mechanism | Primary Token Impact |
| :--- | :--- | :--- | :--- |
| **1** | **Context Pruning** | Identify and omit irrelevant history, stale tool outputs, and unrelated files before reading. | Reduces input context |
| **2** | **Progressive Context Loading** | Keep core Level 1–2 rules inline in `SKILL.md` (<350 tokens) to avoid extra `read_file` tool-call round-trips; load deep references only on demand. | Prevents skill overhead paradox |
| **3** | **Prompt Optimization** | Strip redundant instructions, repeated constraints, and boilerplate phrasing. | Reduces input tokens |
| **4** | **Tool-Call Optimization** | Batch parallel tool calls, reuse prior results in session memory, and forbid duplicate searches. | Cuts multi-turn loop multiplier |
| **5** | **Output Compression** | Deliver concise, high-signal code diffs and direct answers without filler prose or repeating unchanged code blocks. | Reduces expensive output tokens (3x–5x costlier than input) |
| **6** | **Codebase Efficiency** | Query the Tier 2 Project Graph and AST Skeletonizer first; read only targeted `start_line`–`end_line` ranges. | Cuts file discovery tokens by 80%–90% |
| **7** | **Memory Optimization** | Persist compact architectural decisions and symbol locations in `.tokwiz/session-state.json` instead of re-reading files. | Prevents re-discovery across turns |
| **8** | **Reasoning Efficiency** | Match analysis depth and verification steps to task complexity (skip exhaustive exploration on trivial edits). | Reduces hidden reasoning tokens |
| **9** | **KV-Cache & Prefix Hygiene** | Never mutate early conversation history or reorder static system context mid-session; compact only at checkpoints. | Preserves 75%–90% cached input discount |
| **10** | **Subagent Context Isolation** | Delegate exploratory searches (>3 files) or noisy test/log triage to a read-only subagent that returns a <200-token summary. | Prevents main-thread context pollution |

#### 3.3.2 Three Optimization Levels
* **Level 1 — Safe Optimization (Default / Always On):**
  * Zero risk to quality. Enforces ranged file reads, graph-first symbol lookups, tool call batching, cache-prefix preservation, and concise outputs.
* **Level 2 — Adaptive Optimization (Triggered on Medium/High Complexity):**
  * Automatically activates when task blast radius >2 files or context exceeds 25k tokens. Uses AST skeletonization (`scripts/code-skeletonizer.py`), subagent isolation for exploration, and checkpoint conversation summaries.
* **Level 3 — Aggressive Optimization (Opt-In or Hard Budget Cap):**
  * Triggered when user requests maximum savings or approaches budget limits. Compresses large specifications (`references/level3-compression.md`), enforces strict output length limits, and aggressively truncates tool logs.

> [!IMPORTANT]
> **Safety & Quality Invariant:** TokWiz must **never** remove or compress information that affects code correctness, security, user constraints, business logic, or test verification merely to save tokens.

---

### Pillar 4: Deterministic Anti-Wastage Tool Guardrails

Because LLMs occasionally ignore markdown instructions during complex debugging, TokWiz enforces hard guardrails via `PreToolUse` and `PostToolUse` hooks:

1. **Blocked/Gated File Patterns (`hooks/pre-tool-guard.py`):**
   * Automatically blocks or rewrites attempts to read high-waste files: lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`), build outputs (`dist/`, `build/`, `.next/`, `out/`), vendor directories (`node_modules/`, `.venv/`), binary assets, and minified `.min.js` bundles.
2. **Unbounded File Read Interception:**
   * If an agent attempts to read a source file `>300 lines` without specifying `StartLine`/`EndLine`, the guardrail either returns the file's **AST Skeleton** (imports + signatures + line numbers, ~10% token size) or prompts the agent to read the exact line range from the Project Graph.
3. **Shell Output Compression (`scripts/output-filter.py`):**
   * Wraps noisy shell commands (`npm test`, `pytest`, `cargo build`, `git diff`, `docker build`) to strip ANSI escape sequences, duplicate progress bars, passing test noise, and lockfile diffs—keeping only failing assertions, relevant stack frames, and summary counts.

---

### Pillar 5: Statistics & Optimization Analytics Dashboard

TokWiz includes a built-in telemetry engine (`scripts/telemetry.py`) and visual dashboard (`dashboard/`) so users can verify real-world token and cost savings.

#### 3.5.1 Tracked Metrics (`.tokwiz/telemetry.db`)
* **Forecasted Cost vs. Real Cost Variance:**
  * Side-by-side tracking of **Forecasted Tokens & Cost** ($T_{\text{forecast}}, C_{\text{forecast}}$) vs. **Real Billed Tokens & Cost** ($T_{\text{real}}, C_{\text{real}}$) for every prompt turn.
  * Tracks signed Cost Delta ($\Delta C = C_{\text{real}} - C_{\text{forecast}}$), Absolute Percentage Error ($|\Delta C| / C_{\text{real}}$), rolling MAPE, and variance attribution tags (`EXTRA_TOOL_LOOPS`, `CACHE_MISS_DRIFT`, `REASONING_SPIKE`, `UNBOUNDED_TOOL_OUTPUT`).
* **Net Token Savings (Including Skill Overhead):**
  $$\text{Net Savings} = \text{Estimated Unoptimized Baseline} - (\text{Actual Workflow Tokens} + \text{TokWiz Skill/Graph Tokens})$$
* **Cost & Cache Efficiency:**
  * Total `$` spent vs. `$` saved across Claude, Codex, and Gemini.
  * KV Prompt Cache Hit Ratio (`Cached Input Tokens / Total Input Tokens`) and cache savings in `$`.
* **Anti-Wastage Breakdown:**
  * Number of `grep`/`find` loops avoided via Tier 2 Project Graph queries.
  * Tokens saved by blocking lockfiles, skeletonizing large files, and filtering noisy shell outputs.
* **Historical Trends:** Daily/weekly token burn rate, most expensive repositories/files, and optimization level distribution (Level 1 vs. 2 vs. 3).

#### 3.5.2 Dashboard Interfaces
1. **CLI Quick Report (`tokwiz stats`):**
   * Renders rich terminal tables and sparklines showing current session and all-time token savings, **Forecasted vs. Real Cost delta**, MAPE accuracy score, and cache hit rates.
2. **Interactive Local Web Dashboard (`tokwiz dashboard`):**
   * Lightweight zero-build local web server (Python `http.server` / FastAPI + single-page HTML/CSS/Canvas UI) with:
     * KPI Cards: *Total Tokens Saved*, *Net `$` Saved*, *Forecasted vs. Real Cost ($\Delta C$ & MAPE %)*, *Avg Reduction %*, *Cache Hit %*.
     * Charts:
       * *Forecasted Cost vs. Real Billed Cost per Prompt (Dual-Line & Residual Error Bar Chart)*
       * *Baseline Unoptimized vs. Real TokWiz Token Usage Over Time*
       * *Token Breakdown by Phase (Context, Graph, Tool Output, Reasoning, Output)*
       * *Model Cost Comparison (Claude vs. Codex vs. Gemini)*
     * Live Prompt Audit Log: Inspect individual prompts, initial blast-radius forecast, forecasted cost, real billed cost, variance reason, and exact tokens saved per guardrail.

---

## 4. Proposed Repository Structure (`tokwiz/`)

```text
tokwiz/
├── SKILL.md                           # Compact (<350 tokens) core skill router & Level 1-2 rules
├── AGENTS.md                          # Universal always-on bootstrapper for Codex, Claude, Gemini
├── README.md                          # Installation, CLI usage, and quickstart guide
├── pyproject.toml                     # CLI entrypoint (`tokwiz`) & optional tree-sitter deps
│
├── docs/
│   └── requirements/
│       └── product-scope.md           # This Product Scope & Architecture document
│
├── references/                        # Loaded on-demand ONLY when Level 3 or deep rules needed
│   ├── level3-compression.md          # Aggressive spec & context compression templates
│   ├── pricing-matrix.json            # Up-to-date token pricing & cache discounts (Claude/Codex/Gemini)
│   └── safety-and-quality.md          # Quality preservation invariants & red-lines
│
├── scripts/
│   ├── cli.py                         # Unified `tokwiz` CLI (init, predict, graph, skeleton, stats, dashboard)
│   ├── project-graph.py               # Tier 2 Tree-Sitter + Personalized PageRank codebase graph
│   ├── token-estimator.py             # Pre-prompt blast-radius & workflow token/cost forecaster
│   ├── calibration.py                 # Reconciles Forecast vs. Real cost & updates EMA weights
│   ├── code-skeletonizer.py           # AST signature extractor (strips bodies, keeps line numbers)
│   ├── output-filter.py               # Compresses noisy test/build/git shell outputs
│   ├── context-analyzer.py            # Audits transcript bloat, duplicates, and KV-cache stability
│   └── telemetry.py                   # SQLite ledger recording forecasts, actual usage, variance & savings
│
├── hooks/
│   ├── pre-prompt-predict.py          # PreInvocation / UserPromptSubmit hook for zero-token forecast UI
│   ├── pre-tool-guard.py              # PreToolUse hook blocking lockfiles & unbounded >300-line reads
│   └── post-turn-record.py            # PostInvocation / Stop hook reconciling real cost vs. forecast
│
├── adapters/
│   ├── claude/
│   │   ├── settings.json              # Claude Code hook bindings
│   │   └── CLAUDE.md                  # Claude-specific prompt cache & Task subagent adapter
│   ├── codex/
│   │   ├── AGENTS.md                  # Codex CLI rules & tool-use adapter
│   │   └── tokwiz-codex-wrapper.sh    # Pre-prompt forecast & post-run cost reconciliation wrapper
│   ├── gemini/
│   │   ├── hooks.json                 # Gemini CLI / Antigravity native lifecycle hooks
│   │   └── GEMINI.md                  # Gemini context-caching & subagent adapter
│   └── generic/
│       └── cursor-copilot.md          # Adapter for Cursor & GitHub Copilot
│
├── dashboard/
│   ├── server.py                      # Local analytics server (`tokwiz dashboard`)
│   └── index.html                     # Interactive statistics & Forecast-vs-Real cost UI
│
├── tests/                             # Automated unit, hook contract, and replay calibration tests
│   ├── test_project_graph.py
│   ├── test_token_estimator.py
│   ├── test_cost_reconciliation.py
│   ├── test_tool_guardrails.py
│   └── test_hook_contracts.py
│
└── evaluations/
    ├── benchmark-tasks.json           # Repeatable benchmark scenarios across repo sizes
    ├── run-benchmark.py               # A/B runner comparing Baseline vs. TokWiz (net token savings)
    └── evaluation-guide.md            # Methodology for scoring token reduction & quality retention
```

---

## 5. Installation & Day-to-Day User Workflow Across AI Agents

TokWiz is designed for **zero-friction onboarding** (<30 seconds) and **invisible background operation** once installed.

### 5.1 Step 1: Installing `tokwiz`
Users can install the `tokwiz` CLI globally via `uv`, `pipx`, or `pip` (or directly from source):
```bash
# Option A: Global CLI installation (recommended)
uv tool install tokwiz          # or: pipx install tokwiz

# Option B: From repository clone
git clone https://github.com/<org>/tokwiz.git && cd tokwiz
pip install -e ".[treesitter]"
```

### 5.2 Step 2: Wiring TokWiz into AI Agents (`tokwiz init`)
Running `tokwiz init` inside any project (or with `--global` for all projects on the machine) automatically configures the target AI agents:

```bash
# Configure for all supported agents in the current repository
tokwiz init --agent all

# Or install globally across the user's machine
tokwiz init --global --agent claude,codex,gemini
```

What `tokwiz init` sets up behind the scenes:
1. **Builds the initial Tier 2 Project Graph** (`.tokwiz/graph.db`) and initializes `.tokwiz/telemetry.db` (automatically adding `.tokwiz/` to `.gitignore`).
2. **Installs the Skill (`SKILL.md`):**
   * Places/links `skills/tokwiz/SKILL.md` into `.agents/skills/tokwiz/` (for Gemini/Antigravity & Codex) and `.claude/skills/tokwiz/` (for Claude Code).
3. **Wires Lifecycle Hooks & Always-On Rules:**
   * **Claude Code:** Merges `UserPromptSubmit`, `PreToolUse`, and `Stop` hooks into `.claude/settings.json` and appends a 1-line TokWiz rule reference to `CLAUDE.md`.
   * **Gemini CLI / Antigravity:** Writes `.agents/hooks.json` (`PreInvocation`, `PreToolUse`, `PostInvocation`, `Stop`) and links `.agents/rules/tokwiz.md`.
   * **OpenAI Codex CLI:** Updates `AGENTS.md` with TokWiz graph/skeleton instructions and installs the `tokwiz-codex` shell alias/wrapper so prompts are forecasted before execution and reconciled afterward.

### 5.3 Step 3: How the User Uses TokWiz in Daily Workflows

#### Mode A: Automatic Passive Mode (Zero Effort During Coding)
Once initialized, the user simply talks to **Claude Code**, **Codex**, or **Gemini / Antigravity** as normal:
1. **User enters a prompt:** e.g., `"Refactor validateToken to support API key headers and update tests"`
2. **Pre-Prompt Forecast Appears Instantly (<50ms):**
   * Before the model runs, the hook queries `.tokwiz/graph.db`, calculates the blast radius, logs `forecast_id`, and displays the **TokWiz Forecast Banner** (predicted tokens + estimated `$` cost for the active model).
3. **Agent Executes with Guardrails & Graph:**
   * Instead of running 6 `grep`/`find` commands, the agent queries `tokwiz graph query validateToken` (getting exact `file:L30-L52` coordinates and 3 callers in ~340 tokens).
   * If the agent tries to read a 900-line file without line ranges or `package-lock.json`, the `PreToolUse` hook intercepts it and returns a compact AST skeleton or blocks the lockfile.
   * When the agent runs `npm test`, `output-filter.py` compresses 4,000 lines of noisy logs down to the failing assertion.
4. **Post-Turn Real Cost Reconciliation:**
   * When the agent finishes the turn, the `Stop` / `PostInvocation` hook reads the real provider token usage from the session transcript, records **Forecasted Cost vs. Real Cost ($\Delta C$)** into `.tokwiz/telemetry.db`, and updates the self-calibration weights.

#### Mode B: On-Demand CLI & Interactive Commands
Users (and agents) can also run `tokwiz` directly from the terminal at any time:

| Command | Purpose |
| :--- | :--- |
| `tokwiz predict "<prompt>"` | Dry-run forecast showing blast radius, impacted files, token estimate, and `$` cost across Claude, Codex, and Gemini without calling any LLM. |
| `tokwiz stats [--session \| --days 7]` | Terminal summary showing total tokens saved, **Forecasted vs. Real Cost table**, MAPE accuracy, and cache hit rate. |
| `tokwiz dashboard [--port 8420]` | Launches the local web analytics dashboard in the browser. |
| `tokwiz graph query <symbol>` | Inspects the Tier 2 Tree-Sitter PageRank slice for any symbol or file. |
| `tokwiz skeleton <file>` | Outputs the compact AST signature skeleton with line numbers for a file. |
| `tokwiz level <1\|2\|3\|auto>` | Sets the optimization aggressiveness profile (`1=Safe`, `2=Adaptive`, `3=Aggressive`, `auto=Default`). |
| `tokwiz budget --max-prompt-cost 0.25` | Configures a safety gate that asks for user confirmation if a single prompt's forecasted cost exceeds `\$0.25`. |

---

## 6. Testing, Verification & Benchmarking Strategy

To guarantee that TokWiz reliably saves tokens, forecasts accurately, and never breaks host agent workflows, testing is structured into **four verification tiers**:

### 6.1 Tier 1: Deterministic Unit & Component Tests (`pytest`)
* **Project Graph Tests (`tests/test_project_graph.py`):**
  * Verifies Tree-Sitter AST symbol extraction, line-number accuracy (`start_line`–`end_line`), import/call edge resolution across Python, TypeScript/JS, Go, and Rust fixtures, and fallback parser parity.
  * Verifies Personalized PageRank determinism, `--max-tokens` budget enforcement, and `<40ms` incremental SQLite cache invalidation when files change.
* **Token Estimator & Pricing Tests (`tests/test_token_estimator.py`):**
  * Validates complexity classification (Profiles A–E), blast-radius line summation, and pricing calculations (including cached vs. uncached rates and reasoning multipliers for Claude, Codex, and Gemini).
* **Forecast vs. Real Cost Reconciliation Tests (`tests/test_cost_reconciliation.py`):**
  * Tests parsing real usage payloads from Claude Code, Codex CLI, and Gemini/Antigravity transcripts.
  * Verifies $\Delta T$, $\Delta C$, MAPE calculations, variance root-cause tagging (`CACHE_MISS_DRIFT`, `EXTRA_TOOL_LOOPS`, etc.), and EMA calibration updates in `.tokwiz/calibration.json`.
* **Anti-Wastage Guardrail & Filter Tests (`tests/test_tool_guardrails.py`):**
  * Verifies lockfile/artifact blocking, `>300`-line file read interception, AST skeleton generation, and ANSI/stack-trace log compression ratios.

### 6.2 Tier 2: Hook Contract & Fail-Open Resilience Tests (`tests/test_hook_contracts.py`)
* **Strict Schema Compliance:** Pipes synthetic `stdin` hook payloads for Gemini/Antigravity (`PreInvocation`, `PreToolUse`, `PostInvocation`, `Stop`) and Claude Code (`UserPromptSubmit`, `PreToolUse`, `Stop`) into the hook scripts and validates the exact `stdout` JSON schema.
* **Zero-Token Injection Check:** Verifies that `PreInvocation` forecast banners populate `metadata.user_markdown` while keeping model-visible `systemMessage` empty (`""`) when operating in zero-token display mode.
* **Fail-Open & Latency SLA (`<200ms`):** Simulates corrupted SQLite files, missing dependencies, or malformed transcripts to verify that hooks **always fail open** (returning `{}` or `"decision": "allow"` within `<200ms`) and never block or crash the user's AI agent.

### 6.3 Tier 3: Historical Transcript Replay & Forecast Calibration
* Runs `evaluations/replay-transcripts.py` against real recorded agent session transcripts (`.jsonl` logs from Claude Code, Gemini/Antigravity, and Codex).
* For each historical user prompt in a transcript:
  1. Generates TokWiz's pre-prompt forecast ($T_{\text{forecast}}, C_{\text{forecast}}$).
  2. Compares it against the actual tokens and cost recorded in that turn ($T_{\text{real}}, C_{\text{real}}$).
  3. Asserts that after 5–10 turns of EMA self-calibration, **MAPE converges to $\le 20\%$**.

### 6.4 Tier 4: Live A/B Agent Benchmarks & Success Metrics
TokWiz is evaluated on **Net Workflow Savings** (accounting for all tokens used by `SKILL.md` and graph slices) by running paired tasks (**Baseline Agent without TokWiz** vs. **Agent with TokWiz**):

| Benchmark Scenario | Primary Metric Measured | Target Token Reduction | Target Quality Retention |
| :--- | :--- | :--- | :--- |
| **1. Large Codebase Navigation** | Discovery turns, files read, total input tokens | **60% – 85%** | **98%+** |
| **2. Multi-File Feature / Refactor** | End-to-end input + tool + output tokens | **35% – 55%** | **95%+** |
| **3. Long Conversation Session (20+ turns)** | Cumulative context tokens & cache hit rate | **40% – 60%** | **95%+** |
| **4. Failing Test / Build Triage** | Tool output tokens from test runners & stack traces | **50% – 80%** | **98%+** |
| **5. Technical Research & Docs Analysis** | Progressive loading vs. upfront doc ingestion | **35% – 50%** | **95%+** |
| **6. Forecasted vs. Real Cost Accuracy** | $\text{MAPE} = \|C_{\text{real}} - C_{\text{forecast}}\| / C_{\text{real}}$ | **Within ±20%** | N/A |

---

## 7. Phased Implementation Roadmap

### Phase 1: Core Skill, Tier 2 Project Graph & Pre-Prompt Forecaster (MVP)
* Implement compact `SKILL.md` (<350 tokens) with Level 1–3 optimization rules and safety invariants.
* Build `scripts/project-graph.py` with Tree-Sitter multi-language parsing, SQLite caching (`.tokwiz/graph.db`), and Personalized PageRank subgraph slicing.
* Build `scripts/code-skeletonizer.py` for AST signature extraction with exact line numbers.
* Build `scripts/token-estimator.py` + `references/pricing-matrix.json` to forecast input, tool-loop, output tokens, and `$` cost across Claude, Codex, and Gemini using graph blast-radius analysis.
* Add Tier 1 unit tests (`tests/test_project_graph.py`, `tests/test_token_estimator.py`).

### Phase 2: Lifecycle Hooks, Anti-Wastage Guardrails & Cross-Agent Adapters
* Implement `hooks/pre-prompt-predict.py` for automatic zero-token forecast banners on prompt entry.
* Implement `hooks/pre-tool-guard.py` and `scripts/output-filter.py` to block lockfiles, intercept unbounded `>300`-line file reads, and compress verbose shell outputs.
* Package turnkey adapters for **Claude Code** (`.claude/settings.json`), **Codex CLI** (`AGENTS.md` + wrapper), and **Gemini / Antigravity** (`.agents/hooks.json`), plus `tokwiz init` installer.
* Add Tier 2 hook contract and fail-open resilience tests (`tests/test_hook_contracts.py`, `tests/test_tool_guardrails.py`).

### Phase 3: Real-Cost Reconciliation, Self-Calibration & Analytics Dashboard
* Implement `scripts/telemetry.py`, `scripts/calibration.py`, and `hooks/post-turn-record.py` to parse real billed tokens from agent transcripts, compute **Forecasted Cost vs. Real Cost variance ($\Delta C$, MAPE)**, tag drift root causes, and auto-calibrate future predictions.
* Build `tokwiz stats` CLI reporter and `tokwiz dashboard` (`dashboard/server.py` + `dashboard/index.html`) visualizing token savings, Forecast-vs-Real cost charts, and per-prompt audit trails.
* Add Tier 3 transcript replay & calibration tests (`tests/test_cost_reconciliation.py`).

### Phase 4: Automated A/B Benchmarking & Ecosystem Distribution
* Build `evaluations/run-benchmark.py` and `evaluations/benchmark-tasks.json` to run paired A/B evaluations (Baseline vs. TokWiz) verifying Net Token Savings and test-suite pass rates.
* Publish `tokwiz` package and documentation for one-command installation across repositories.
