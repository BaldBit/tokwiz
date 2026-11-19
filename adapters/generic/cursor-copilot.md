# TokWiz Adapter for Cursor & GitHub Copilot

1. **Repo Map & Graph First:** Run `tokwiz map` (MCP `tokwiz_map`) for a `<220`-token codebase architecture overview, and `tokwiz slice <symbol>` / `tokwiz graph query <symbol> --max-tokens 400` to locate definitions, callers, and callees with exact line numbers before running workspace-wide grep.
2. **Ranged File Reads:** For files >150–300 lines, inspect `tokwiz skeleton <file>` (MCP `tokwiz_skeleton`) first and read only the target `StartLine`–`EndLine` range.
3. **Skip High-Waste Files:** Never read lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`), `node_modules/`, `dist/`, `build/`, or `.min.js`.
4. **Git-Diff AST Slice & Impacted Tests:** Use `tokwiz diff-slice` (MCP `tokwiz_diff_slice`) to map modified lines to enclosing AST symbols and callers (`<500` tokens), and run `tokwiz test` (MCP `tokwiz_test`) or wrap verbose outputs with `tokwiz filter -- <command>`.
5. **Context-Rot Recovery (`tokwiz resume`) & Concise Diffs:** After `/clear` or context reset, call `tokwiz resume` (MCP `tokwiz_resume`) to reload `.tokwiz/checkpoint.md` (`<200` tokens), and return minimal diffs without repeating unchanged code.
