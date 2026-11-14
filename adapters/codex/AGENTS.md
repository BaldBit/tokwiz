# TokWiz Adapter for OpenAI Codex CLI (`AGENTS.md`)

- Follow `.agents/skills/tokwiz/SKILL.md` for all coding, debugging, and refactoring tasks.
- **Graph First:** Run `tokwiz graph query <symbol> --max-tokens 400` before shell `grep`/`rg`/`find`.
- **Ranged Reads:** Run `tokwiz skeleton <file>` on files >300 lines, then read specific line ranges (`sed -n 'Start,Endp' <file>`).
- **Shell Output Compression:** Pipe test and build runs through `tokwiz filter` (e.g., `pytest | tokwiz filter`).
- **Batching:** Combine related read/check commands to minimize multi-turn reasoning overhead.
