# TokWiz Always-On Agent Protocol (`AGENTS.md`)

Follow the TokWiz token & context optimization protocol on all tasks while preserving 100% code correctness, security, and test verification:

1. **Graph & Repo Map First:** Before running `grep`, `find`, or `ls`, use `tokwiz map` (MCP `tokwiz_map`) for a `<220`-token codebase architecture overview, and query `python3 scripts/project-graph.py query <symbol> --max-tokens 400` (or `tokwiz slice <symbol>` / MCP `tokwiz_slice`) for exact `file:Lstart-Lend` definitions, callers, and callees.
2. **Ranged File Reads:** Never read files >300 lines uncompressed (>150 lines for JSON/YAML/TOML/SQL). Run `python3 scripts/code-skeletonizer.py <file>` (or `tokwiz skeleton <file>` / MCP `tokwiz_skeleton`) first, then read only targeted `StartLine`–`EndLine` ranges.
3. **Skip High-Waste Files:** Never read lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`), `node_modules/`, `dist/`, `build/`, or `.min.js`.
4. **Diff-Slice, Impacted Tests & Filtered Logs:** Use `tokwiz diff-slice` (MCP `tokwiz_diff_slice`) to map `git diff` hunks to AST symbols, 1-hop callers, and impacted tests in `<500` tokens; run `tokwiz test` (MCP `tokwiz_test`) or pipe verbose commands through `python3 scripts/output-filter.py`.
5. **Context-Rot Recovery (`tokwiz resume`) & Cache Hygiene:** Batch parallel tool calls, reuse session findings without re-reading unchanged files, keep early conversation history immutable for KV-cache hits, and after `/clear` or `/compact` run `tokwiz resume` (MCP `tokwiz_resume`) to reload `.tokwiz/checkpoint.md` in `<200` tokens.
