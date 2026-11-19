# TokWiz Adapter for OpenAI Codex CLI (`AGENTS.md`)

- Follow `.agents/skills/tokwiz/SKILL.md` for all coding, debugging, and refactoring tasks.
- **Repo Map & Graph First:** Run `tokwiz map` for `<220`-token repo orientation, and `tokwiz slice <symbol>` / `tokwiz graph query <symbol> --max-tokens 400` before shell `grep`/`rg`/`find`.
- **Ranged Reads:** Run `tokwiz skeleton <file>` on files >150–300 lines, then read specific line ranges (`sed -n 'Start,Endp' <file>`).
- **Diff-Slice & Impacted Tests:** Run `tokwiz diff-slice` (`<500` tokens) to inspect modified AST symbols, 1-hop callers, and impacted tests, and run `tokwiz test` or pipe test runs through `tokwiz filter`.
- **Context-Rot Recovery (`tokwiz resume`) & Batching:** After resetting context, run `tokwiz resume` to reload `.tokwiz/checkpoint.md` (`<200` tokens), and batch related commands to minimize multi-turn reasoning overhead.
