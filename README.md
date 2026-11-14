# TokWiz (`tokwiz`)

> *Think efficiently. Forecast ahead. Use fewer tokens.*

**TokWiz** is an intelligent, cross-agent token optimization layer, Tier 2 Tree-Sitter/PageRank codebase graph, pre-prompt cost forecaster, anti-wastage guardrail suite, and usage analytics dashboard for **Claude Code**, **OpenAI Codex CLI**, **Google Gemini CLI / Antigravity**, **Cursor**, and **GitHub Copilot**.

## Quickstart

```bash
# Install editable CLI
pip install -e .

# Initialize TokWiz in any repository for all supported AI agents
python3 scripts/cli.py init --agent all

# Forecast prompt tokens, blast radius, and cross-provider $ cost
python3 scripts/cli.py predict "Refactor validateToken to support API key headers"

# Query Tier 2 Project Graph (Personalized PageRank slice)
python3 scripts/cli.py graph query validateToken --max-tokens 400

# Extract compact AST signature skeleton with exact line numbers
python3 scripts/cli.py skeleton scripts/project-graph.py

# View terminal token savings & Forecast-vs-Real Cost MAPE report
python3 scripts/cli.py stats

# Launch interactive local web analytics dashboard
python3 scripts/cli.py dashboard --port 8420

# Run paired A/B evaluation benchmarks
python3 scripts/cli.py benchmark
```

## CLI Commands

| Command | Description |
| :--- | :--- |
| `tokwiz init [--agent all\|claude,codex,gemini,cursor] [--global]` | Wires `SKILL.md`, lifecycle hooks, rules, and initializes `.tokwiz/graph.db` |
| `tokwiz predict "<prompt>"` | Ahead-of-time workflow token & `$` cost forecast across Claude, Codex, and Gemini |
| `tokwiz graph <build\|query>` | Builds or queries the SQLite Personalized PageRank symbol graph |
| `tokwiz skeleton <file>` | Outputs compact AST signatures with `Lstart-Lend` coordinates |
| `tokwiz filter [-- <cmd>]` | Strips ANSI, progress bars, passing test noise, and lockfile diffs |
| `tokwiz stats [--session \| --days N]` | Terminal report of Net Tokens Saved, Forecast vs. Real Cost ($\Delta C$), and MAPE |
| `tokwiz dashboard [--port 8420]` | Launches the interactive local web analytics dashboard |
| `tokwiz benchmark [--json]` | Runs the 6-scenario paired A/B evaluation suite |
| `tokwiz level <1\|2\|3\|auto>` | Sets optimization aggressiveness profile |
| `tokwiz budget --max-prompt-cost <usd>` | Sets per-prompt forecasted cost alert/gate threshold |
