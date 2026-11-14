# TokWiz Always-On Agent Protocol (`AGENTS.md`)

Follow the TokWiz token optimization protocol on all tasks while preserving 100% code correctness, security, and test verification:

1. **Graph First:** Before running `grep` or `find`, query `python3 scripts/project-graph.py query <symbol> --max-tokens 400` (or `tokwiz graph query <symbol>`) for exact `file:Lstart-Lend` definitions, callers, and callees.
2. **Ranged File Reads:** Never read files >300 lines uncompressed. Run `python3 scripts/code-skeletonizer.py <file>` (or `tokwiz skeleton <file>`) first, then read only targeted `StartLine`–`EndLine` ranges.
3. **Skip High-Waste Files:** Never read lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`), `node_modules/`, `dist/`, `build/`, or `.min.js`.
4. **Filter Noisy Logs:** Pipe or wrap verbose test/build commands with `python3 scripts/output-filter.py` (or `tokwiz filter -- <cmd>`).
5. **Batch & Cache Hygiene:** Batch parallel tool calls, reuse session findings without re-reading unchanged files, and keep early conversation history immutable to preserve KV-cache hits.
