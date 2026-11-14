# TokWiz Adapter for Gemini CLI & Antigravity (`GEMINI.md`)

- Follow `.agents/skills/tokwiz/SKILL.md` for token optimization and safety invariants.
- **Graph First:** Query `tokwiz graph query <symbol> --max-tokens 400` before running broad searches.
- **Ranged File Reads:** For files >300 lines, run `tokwiz skeleton <file>` first, then call `read_file` with `StartLine` and `EndLine`.
- **Subagent Isolation:** Delegate broad exploration (>3 files) to the `research` subagent and request a <200-token summary.
- **Context Caching:** Keep system instructions and early conversation turns unchanged to maximize Gemini context cache hit rates.
