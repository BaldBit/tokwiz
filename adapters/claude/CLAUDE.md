# TokWiz Adapter for Claude Code (`CLAUDE.md`)

- Load `.claude/skills/tokwiz/SKILL.md` for token-efficient navigation and output rules.
- **Graph First:** Run `tokwiz graph query <symbol> --max-tokens 400` before `Grep` or `Glob`.
- **Ranged File Reads:** Use `tokwiz skeleton <file>` on files >300 lines, then pass `offset` and `limit` to `Read`.
- **Subagent Isolation:** Use the `Task` tool for broad multi-file exploration (>3 files) or verbose test triage so main-thread prompt cache prefixes remain compact.
- **KV-Cache Preservation:** Keep system instructions and early turns static to maintain 90% Anthropic prompt cache read discounts.
