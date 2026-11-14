# TokWiz Safety & Quality Preservation Invariants

## Non-Negotiable Red Lines
1. **Correctness Over Compression:** Never truncate, summarize, or skip reading lines containing active business logic, edge-case handling, type contracts, or error paths being modified.
2. **Security Preservation:** Never omit authentication checks, permission boundaries, input validation, cryptographic parameters, or SQL/command parameterization.
3. **Exact Line Coordinates:** When editing code after reading an AST skeleton or graph slice, always read the exact target function body (`StartLine`–`EndLine`) before applying changes.
4. **Verification Integrity:** Never skip running relevant unit/integration tests when modifying behavior; filter noisy passing output rather than skipping verification.
