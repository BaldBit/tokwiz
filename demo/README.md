# TokWiz Manual A/B Testing Sandbox (`demo/`)

This folder contains a one-command generator (`setup-ab-demo.py`) that builds two **identical** multi-file projects side-by-side so you can manually test and compare an AI agent **without TokWiz** vs. **with TokWiz**.

## 1. Generate the Two Projects

```bash
python3 demo/setup-ab-demo.py
```

This creates:
* `demo/workspace/project-baseline/` — Standard project **without** TokWiz.
* `demo/workspace/project-tokwiz/` — Identical project **with** `tokwiz init --agent all` pre-installed (`.tokwiz/graph.db`, `SKILL.md`, lifecycle hooks, and guardrails).

*(Note: `demo/workspace/` is git-ignored so your test runs never dirty the repository.)*

---

## 2. Run the Exact Same Prompts in Both Projects

Open two sessions in your AI coding agent (one rooted in `demo/workspace/project-baseline` and one in `demo/workspace/project-tokwiz`) and run these 3 prompts:

### Prompt 1 — Symbol Navigation & Callers
> `"Find where validate_token is defined, list every function that calls it and every function it calls, and explain the authentication flow."`
* **What happens in `project-tokwiz`:** Queries `tokwiz graph query validate_token` (~120 tokens) instead of running multiple `grep`/`find` loops.

### Prompt 2 — Multi-File Refactor + Large File Read (`>300` Lines)
> `"Add an optional allow_api_key: bool = False parameter to validate_token in session.py (allowing tokens starting with 'ak_'), update require_auth and process_checkout, and check calculate_order_total in order_engine.py."`
* **What happens in `project-tokwiz`:** Because `src/services/order_engine.py` is `>320` lines, the `PreToolUse` guardrail intercepts any full-file read with its compact AST Skeleton and directs the agent to read only `L1-L15`.

### Prompt 3 — Test Execution & Lockfile Protection
> `"Run the unit tests in tests/test_checkout.py and add a test for allow_api_key=True."`

---

## 3. Inspect Savings & Visualize the Project Graph

From the repository root, run:

```bash
# View terminal savings and Forecast-vs-Real Cost MAPE for project-tokwiz
python3 scripts/cli.py stats --root demo/workspace/project-tokwiz

# Launch the Interactive Web Dashboard & Project Graph Visualizer
python3 scripts/cli.py dashboard --root demo/workspace/project-tokwiz --port 8420
```
