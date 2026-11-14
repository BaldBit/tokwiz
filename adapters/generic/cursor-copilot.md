# TokWiz Adapter for Cursor & GitHub Copilot

1. **Graph First:** Query `python3 scripts/project-graph.py query <symbol> --max-tokens 400` to locate definitions, callers, and callees with exact line numbers before running workspace-wide grep.
2. **Ranged File Reads:** For files >300 lines, inspect `python3 scripts/code-skeletonizer.py <file>` first and read only the target `StartLine`–`EndLine` range.
3. **Skip High-Waste Files:** Never read lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`), `node_modules/`, `dist/`, `build/`, or `.min.js`.
4. **Filter Verbose Outputs:** Wrap test or build commands with `python3 scripts/output-filter.py -- <command>`.
5. **Concise Diffs:** Return minimal, high-signal diffs without repeating unchanged code.
