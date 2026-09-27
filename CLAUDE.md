# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

LLM Release Gate compares a **candidate** LLM configuration against the approved **baseline** on the same frozen synthetic suite and load profile, then emits a rule-based recommendation: `PROMOTE`, `HOLD`, `REJECT`, `ROLLBACK` (canary only), or `INVALID` (broken run).

- The **gate is the product**. Alert-to-JSON triage is only the demo workload.
- `architecture.md` explains how the system and the built code work; keep it current when a component lands or changes.
- `plan.md` is the source of truth for scope, metrics, policy, and phases. Read the relevant section before changing behavior; update `plan.md` when a design decision changes.
- Current status: Phases 0–2 code done (`starter-v1` reviewed and frozen); **Phase 3 v1** (scorers, stats, metrics, `gate score`/`gate replay`) done. Next: the first real Colab run (plan §17). Cost metrics are deferred to Phase 4, so gate with `policies/policy_v2.yaml` (v1 without cost) until then. `memory.md` has the latest state. Open decisions are listed in `plan.md` §18. Don't settle them silently; ask.
- Generator rules: labels are derived, never set by hand (severity from the symptom, category from the causes). Anything a variant borrows must come from the same split. Every change must keep `validate_cases` at zero issues and the cross-split similarity margin. Never read release-split cases while designing prompts or scorers.

## Non-negotiable rules

1. **Synthetic data only.** Never add real incidents, logs, customer prompts, credentials, or personal data to suites, fixtures, or tests.
2. **No LLM makes the release decision.** The gate is a deterministic function of saved metrics and a versioned policy. An optional LLM judge may produce a metric, but never the decision alone.
3. **Never tune on the release benchmark.** Do not read release-benchmark cases to design prompts, scorers, labels, or generator rules. Use the development split. Do not edit a frozen suite version; create a new version instead.
4. **Every run must be replayable.** Record hashes of the suite, scorers, policy, prompts, and model digests in the run manifest. `gate replay <run_id>` must reproduce the decision exactly.
5. **No automatic deployment or rollback of real services.** Rollback is a report recommendation only.
6. **Treat model output as untrusted data.** Never execute, eval, or shell out with anything a model returned, including `next_check` commands.
7. **Report honestly.** Results are "on the synthetic benchmark", never claims about production quality. Every score carries its sample size.

## Gate semantics (keep consistent with plan.md §8)

Rules are evaluated in this order; the first match wins:

| Order | Condition | Outcome |
|---|---|---|
| 1 | Baseline unhealthy, baseline replay outside tolerance, infra error rate above limit, manifest mismatch | `INVALID` |
| 2 | Any hard limit fails with confidence, or any safety limit exceeded | `REJECT` (pre-deploy) / `ROLLBACK` (canary) |
| 3 | Any limit inconclusive, or a protected slice below `minimum_cases_per_slice` | `HOLD` |
| 4 | All limits pass | `PROMOTE` |

- Quality limits are **paired non-inferiority checks**. Compute a paired bootstrap CI on candidate minus baseline. The limit passes if the CI is entirely above `-max_drop`, fails if it is entirely below, and is inconclusive otherwise. Use a fixed bootstrap seed from the policy.
- Latency and cost have both **absolute** and **relative** limits.
- Error rate is gated **before retries**.
- Safety limits (unsafe `next_check` commands, prompt-injection compliance) are zero-tolerance.
- Every decision lists the specific rules that triggered it.
- CLI exit codes: `0` promote, `1` hold, `2` reject/rollback, `3` invalid, `4` no decision produced: input, usage, or internal error. Never let argparse exit with 2 or an exception escape `main()` (exit 1 = HOLD).
- Absolute limits use the candidate point estimate. Comparative limits use the delta CI, and without a CI they are inconclusive. A configured limit with no metric is INVALID. Full rules are in `plan.md` §8 and `src/release_gate/gate/engine.py`.

## Architecture conventions

- `gate(metrics, policy) -> decision` is a **pure function**: no network, no model calls, no clock, no randomness beyond the policy's seed. Keep I/O out of `gate/`, `stats/`, and `scorers/`.
- Scorers are deterministic and versioned. Changing scoring behavior means bumping the scorer version.
- Adapters speak the **OpenAI-compatible** API only. Don't add vendor-specific logic outside `adapters/`.
- Baseline and candidate get identical cases and settings. When they share hardware, interleave requests after a warm-up.
- Policies, suites, and prompts are versioned files. Code never hard-codes thresholds.

Proposed layout (confirm in Phase 0; see `plan.md` §18):

```text
policies/  suites/  prompts/  models/
src/release_gate/{generator,adapters,runner,scorers,stats,gate,report}/  cli.py
tests/fixtures/  proxy/  deploy/  .github/workflows/
```

## Stack

- **MVP:** Python, pydantic, httpx (async), pytest, JSON + SQLite storage, Jinja reports. Serving is **vLLM on a Colab A100** for real runs and a local mock OpenAI-compatible endpoint for development and tests (`plan.md` §9 Execution environments).
- **Colab rules:** each evaluation is a batch job. The runner runs inside the notebook against `localhost` (no tunnels). Baseline and candidate share one session and one GPU. The manifest records GPU and vLLM versions. Never write code that needs a GPU for local tests or CI.
- **Models:** chosen through editable specs in `models/*.yaml`, loaded by `release_gate.registry` by name. Never hard-code model ids, ports or sampling settings in code. Current pair: `ministral-3-8b-instruct-fp8` (baseline) vs `qwen3-8b-fp8` (candidate, `enable_thinking: false`); `llama-3.1-8b-instruct-fp8` (gated) waits for Hugging Face access. All revisions are pinned to commit SHAs. Specs use FP8 weights (`quantization: fp8`, bf16 activations), so the pair runs concurrently on a 40 GB A100. `dtype` is activation precision only. HF tokens live only in the Colab secret `HF_TOKEN`.
- **Later phases only:** FastAPI, OpenTelemetry, Prometheus/Grafana, MLflow, Docker, kind/k3d, GitHub Actions, UI.
- Don't introduce a later-phase dependency before the first end-to-end report works (`plan.md` §17).

## Development environment

- Windows 11. The shell tools are PowerShell 5.1 and Git Bash. Use forward slashes in code paths and `pathlib` in Python.
- Virtualenv: `.venv/` (Python 3.11), package installed editable. Run tests with `.venv/Scripts/python -m pytest -q`. Lint with `.venv/Scripts/python -m ruff check .` and `ruff format --check .`.
- Gate on a saved metrics file: `.venv/Scripts/gate decide --metrics <file> --policy policies/policy_v1.yaml [--out <json>] [--report <md>]`
- Model specs (after delegated task T008): `gate models list`, `gate models show <name>`, `gate models serve-cmd <name> --port 8001`
- Suites: `gate suite generate --config suites/configs/<v>.yaml`, `gate suite validate|show|freeze <suite_dir>`, `gate suite review <suite_dir> --case <id> --status approved --reviewer <name>`. Exit 1 from generate/validate means the suite has issues (not HOLD). Claude never runs `show --split release`.
- Runs: `gate mock serve --persona <p> --served-model <spec name> --port <n>` (local mock endpoint, loopback only); `gate run --suite <dir> --baseline <spec> --candidate <spec> --baseline-url <url> --candidate-url <url>` writes `runs/<run_id>/` (exit 0 = saved, not a decision). Real runs use `notebooks/evaluate.ipynb` on Colab.
- Score and replay: `gate score <run_dir> --policy policies/policy_v2.yaml` (writes scores, metrics, decision, report and a policy copy into the run dir; exit code = decision); `gate replay <run_dir|run_id> [--policy <file>]` (exit 3 if the saved evaluation is not reproduced; with --policy it is a what-if that saves nothing).

## Testing expectations

- Every gate outcome and precedence rule has a fixture in `tests/fixtures/` with its expected decision and triggered rules.
- An A/A comparison (baseline vs. itself) must never produce `REJECT`.
- Same generator seed → byte-identical suite.
- Replaying a saved run → identical decision.
- Adapter failure modes (timeouts, invalid JSON, empty output, 5xx) are tested through the fault-injection proxy or mocks, never against a hosted provider.
- Tests must not require a running model server unless marked as integration tests.

## Project memory

Read `memory.md` at the start of each session. Record new preferences and project state there, **not** in the global Claude memory directory.

## Task delegation (user rule)

Claude Code does **complex work only**. The user runs a cheaper model for simple work.

- Before doing a task, decide whether a simpler model could do it reliably from a written prompt. If so, **don't do it**. Add an entry to `delegated_tasks.json` (`file`, `prompt`, `context_files`, `depends_on`, `review_by_claude`, `status`) and tell the user it was queued.
- **Simple (delegate):** boilerplate and config (`pyproject.toml`, `.gitignore`, Dockerfiles, CI YAML skeletons); pydantic models or schemas fully specified in `plan.md`; README and doc sections; repetitive fixtures or scenario templates once one example exists; thin CLI or API wiring around existing functions; mechanical renames and formatting.
- **Complex (Claude does it):** gate engine precedence logic; statistics (bootstrap, McNemar, non-inferiority); scorers with edge cases (entity extraction, command allowlist, injection checks); generator invariants and split and leakage design; async runner, interleaving, and load logic; run manifest and replay; architecture and design decisions; debugging; reviewing delegated output.
- Delegated prompts must stand alone. The other model has no conversation context, so state exact paths, field names, conventions from this file, and acceptance criteria.
- Queue a task only after the Claude-owned work it depends on exists.

## Working style

- Build the smallest complete path first: gate engine + fixtures → 30-case suite → runner → report. Then expand.
- Keep changes scoped to the current phase in `plan.md` §12.
- When adding a metric or policy field, update in one change: the policy schema, the gate rule, a fixture, the report template, and `plan.md` §8.
