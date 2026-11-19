# TokWiz Adapter for Gemini CLI & Antigravity (`GEMINI.md`)

- Follow `.agents/skills/tokwiz/SKILL.md` for token optimization, context lifecycle management, and safety invariants.
- **Repo Map & Graph First:** Call `tokwiz_map` (`tokwiz map`) for `<220`-token repo orientation, and `tokwiz_slice` / `tokwiz_graph_query` (`tokwiz graph query <symbol> --max-tokens 400`) before broad searches.
- **Ranged File Reads:** For code files >300 lines (or data/config files >150 lines), run `tokwiz_skeleton` (`tokwiz skeleton <file>`) first, then call `read_file` with `StartLine` and `EndLine`.
- **Git-Diff AST Slice & Impacted Tests:** Use `tokwiz_diff_slice` (`tokwiz diff-slice`) to map modified lines to enclosing AST symbols, 1-hop callers, and impacted tests (`<500` tokens), and run `tokwiz_test` (`tokwiz test`).
- **Context-Rot Recovery (`tokwiz_resume`):** After `/clear` or `/compact` (or when `CONTEXT_BLOAT` `>100k` tok/step occurs), call `tokwiz_resume` (`tokwiz resume`) to reload the auto-updated `<200`-token working memory checkpoint (`.tokwiz/checkpoint.md`).
- **Subagent Isolation & Context Caching:** Delegate broad exploration (>3 files) to the `research` subagent (<200-token summary) and keep early conversation turns immutable to maximize Gemini context cache hit rates.
