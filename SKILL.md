---
name: tokwiz
description: Token optimization, Tier 2 codebase graph, ranged reads, and cost forecasting.
---

# TokWiz Optimization Rules

**Safety Invariant:** Never omit details affecting correctness, security, user constraints, business logic, or test verification.

## Level 1: Safe (Always On)
1. **Graph First:** Run `tokwiz graph query <sym>` or `tokwiz slice <sym>` before `grep`/`find`.
2. **Ranged Reads:** Skeletonize code >150L or JSON/YAML/TOML/SQL >150L via `tokwiz skeleton <file>` before ranged reads.
3. **Skip Waste:** Ignore lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`), `node_modules/`, `dist/`, `build/`, `.min.js`.
4. **Batch & Reuse:** Batch parallel calls. Never re-read unchanged files or repeat searches.
5. **Impacted Tests & Checkpoints:** Run `tokwiz test` for diff-driven tests and `tokwiz checkpoint` before `/compact`.
6. **Concise & Cache-Safe:** Emit minimal diffs; keep early context immutable.

## Level 2: Adaptive (>2 Files or >25k Context)
- Extract signatures via `tokwiz skeleton <file>` before editing.
- Delegate >3-file exploration or noisy test triage to subagents (<200-token summary).
- Cache symbol coords in `.tokwiz/session-state.json`.

## Level 3: Aggressive (Opt-In)
- Load `references/level3-compression.md` only when Level 3 is active.
