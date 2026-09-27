# Example release reports

| Example | Outcome | What changed vs. the passing run | Files |
| --- | --- | --- | --- |
| `promote` | PROMOTE | Only the run ID changes. | `promote_metrics.json`; `promote.decision.json`; `promote.report.md` |
| `hold_quality` | HOLD | Accuracy is lower and its CI straddles the 2-point non-inferiority margin, so the evidence is inconclusive. | `hold_quality_metrics.json`; `hold_quality.decision.json`; `hold_quality.report.md` |
| `reject_latency` | REJECT | p95 latency doubles, remaining under the 4000 ms absolute ceiling but far beyond the 25% relative-regression limit. | `reject_latency_metrics.json`; `reject_latency.decision.json`; `reject_latency.report.md` |

Each report is generated from its corresponding metrics fixture with `policies/policy_v1.yaml`.
