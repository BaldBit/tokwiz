# TokWiz A/B Evaluation & Benchmarking Guide

## 1. Net Workflow Savings Formula
Every TokWiz benchmark accounts for the full token overhead of `SKILL.md` and Tier 2 Project Graph slices:

$$\text{Net Savings} = \text{Unoptimized Baseline Tokens} - \left(\text{Actual Workflow Tokens} + \text{TokWiz Skill/Graph Overhead Tokens}\right)$$

## 2. Running the Automated Benchmark Suite

```bash
# Run the complete 6-scenario A/B evaluation suite
python3 scripts/cli.py benchmark

# Output machine-readable JSON metrics
python3 scripts/cli.py benchmark --json

# Replay historical transcripts to evaluate Forecast-vs-Real Cost MAPE convergence
python3 evaluations/replay-transcripts.py <path-to-turns.jsonl>
```

## 3. Target Acceptance Criteria

| Scenario | Target Net Token Reduction | Target Quality Retention |
| :--- | :--- | :--- |
| **1. Large Codebase Navigation** | 60% – 85% | ≥ 98% |
| **2. Multi-File Feature / Refactor** | 35% – 55%+ | ≥ 95% |
| **3. Long Conversation Session (20+ turns)** | 40% – 60%+ | ≥ 95% |
| **4. Failing Test / Build Triage** | 50% – 80%+ | ≥ 98% |
| **5. Technical Research & Docs Analysis** | 35% – 50%+ | ≥ 95% |
| **6. Forecasted vs. Real Cost Accuracy** | MAPE ≤ 20% after EMA calibration | N/A |
