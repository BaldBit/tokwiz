# Level 3 Aggressive Token Compression Rules

Activate **only** when explicitly requested (`tokwiz level 3`) or when prompt cost approaches a configured hard budget cap.

1. **Graph & Map-Only Navigation:** Start with `tokwiz map` (`<220` tokens), then rely strictly on `tokwiz slice <symbol>` / `tokwiz graph query <symbol> --max-tokens 250` and `tokwiz skeleton <file>`. Read only the exact function lines being edited (`±5` lines context).
2. **Diff-Slice & Impacted Tests:** Use `tokwiz diff-slice` (`<500` tokens) instead of raw `git diff`, and `tokwiz test` for 1-line pass receipts.
3. **Strict Output Cap:** Limit prose responses to ≤3 bullet points (<100 tokens). Emit code edits only via tool calls without repeating code blocks in chat responses.
4. **Aggressive Log Truncation:** Cap command outputs to the first failing assertion and top 5 stack frames (<250 tokens).
5. **Checkpoint Reset & Spec Compression:** Whenever context exceeds `100k` tok/step (`CONTEXT_BLOAT`), `/clear` and reload `.tokwiz/checkpoint.md` via `tokwiz resume` (`<200` tokens); store compact `SYMBOL -> INPUT -> OUTPUT -> INVARIANT` tuples in `.tokwiz/session-state.json`.
