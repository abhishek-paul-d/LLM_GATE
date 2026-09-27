# LLM Release Gate

This project compares a candidate LLM configuration against the approved baseline on a frozen synthetic benchmark and load profile. It returns a rule-based PROMOTE / HOLD / REJECT / ROLLBACK / INVALID recommendation with the evidence behind it.

## Status

Phase 0 of [plan.md](plan.md): the gate engine, policy file, and fixture tests exist. The synthetic generator, evaluation runner, scorers, statistics, and load tests are not built yet.

## How the gate decides

| Precedence | Condition | Outcome |
| --- | --- | --- |
| 1 | Baseline or run validity fails, or a configured metric is missing | INVALID |
| 2 | A hard limit fails with confidence, or a safety limit is exceeded | REJECT before deploy; ROLLBACK in canary mode |
| 3 | A limit is inconclusive, or a protected slice has too few cases | HOLD |
| 4 | All configured limits pass | PROMOTE |

- Absolute limits compare the candidate's point estimate.
- Comparative limits use the paired confidence interval of candidate minus baseline. Inconclusive evidence leads to HOLD.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | PROMOTE |
| 1 | HOLD |
| 2 | REJECT or ROLLBACK |
| 3 | INVALID |
| 4 | No decision produced (input, usage, or internal error) |

## Quick start

Run these commands in Windows PowerShell from the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\gate decide --metrics tests/fixtures/gate/pass_metrics.json --policy policies/policy_v1.yaml --report runs/example.report.md
```

## Repository layout

```text
policies/                  Versioned gate policies
src/release_gate/gate/     Gate models and decision engine
src/release_gate/report/   Markdown report renderer and template
src/release_gate/schemas/  Synthetic triage output schema
src/release_gate/cli.py    Saved-metrics CLI
tests/                     Gate, schema, report, and CLI tests
docs/examples/             Generated decisions and reports
plan.md                    Project design and phased plan
CLAUDE.md                  Project conventions and scope
```

## Scope and limitations

The benchmark data is synthetic. Results describe behaviour on that benchmark and do not establish production quality or safety. Rollback is a recommendation only; it is never automatic.

See [plan.md](plan.md) for the full design and [docs/examples/README.md](docs/examples/README.md) for sample reports.
