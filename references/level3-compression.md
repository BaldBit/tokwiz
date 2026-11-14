# Level 3 Aggressive Token Compression Rules

Activate **only** when explicitly requested (`tokwiz level 3`) or when prompt cost approaches a configured hard budget cap.

1. **Graph-Only Navigation:** Rely strictly on `tokwiz graph query <symbol> --max-tokens 250` and `tokwiz skeleton <file>`. Read only the exact function lines being edited (`±5` lines context).
2. **Strict Output Cap:** Limit prose responses to ≤3 bullet points (<100 tokens). Emit code edits only via tool calls without repeating code blocks in chat responses.
3. **Aggressive Log Truncation:** Cap command outputs to the first failing assertion and top 5 stack frames (<250 tokens).
4. **Spec Compression:** Convert verbose requirement docs into compact `SYMBOL -> INPUT -> OUTPUT -> INVARIANT` tuples in `.tokwiz/session-state.json`.
