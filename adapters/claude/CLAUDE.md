# TokWiz Adapter for Claude Code (`CLAUDE.md`)

- Load `.claude/skills/tokwiz/SKILL.md` for token-efficient navigation, context lifecycle, and output rules.
- **Repo Map & Graph First:** Call `mcp__tokwiz__tokwiz_map` (`tokwiz map`) for `<220`-token repo orientation, and `mcp__tokwiz__tokwiz_slice` / `tokwiz_graph_query` (`tokwiz graph query <symbol> --max-tokens 400`) before `Grep` or `Glob`.
- **Ranged File Reads:** Use `mcp__tokwiz__tokwiz_skeleton` (`tokwiz skeleton <file>`) on files >150–300 lines, then pass `offset` and `limit` to `Read`.
- **Git-Diff AST Slice & Impacted Tests:** Call `mcp__tokwiz__tokwiz_diff_slice` (`tokwiz diff-slice`) for `<500`-token diff AST/caller context and `mcp__tokwiz__tokwiz_test` (`tokwiz test`) for impacted tests.
- **Context-Rot Recovery (`tokwiz_resume`):** After `/clear` or `/compact`, call `mcp__tokwiz__tokwiz_resume` (`tokwiz resume`) to reload `.tokwiz/checkpoint.md` in `<200` tokens without re-reading files.
- **Subagent Isolation & KV-Cache Preservation:** Use the `Task` tool for broad exploration (>3 files) and keep early turns static to maintain 90% Anthropic prompt cache read discounts.
