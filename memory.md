# Project memory

Notes for Claude Code for this project only. Keep entries short, date them, and update or remove entries that stop being true.

## User preferences

- **Delegate simple tasks** (2026-09-27): Only do complex work. When a simpler model could do a task reliably, don't implement it. Add an entry to `delegated_tasks.json` with the target file and a self-contained prompt, then tell the user it was queued. The user runs a cheaper model for simple work to save cost. The criteria are in `CLAUDE.md` under "Task delegation".
- **Memory location** (2026-09-27): Store memories for this project in this file only. Do not write to the global Claude memory directory.

## Project state

- 2026-09-27: Phase 0 is complete. Present: gate contract and engine (`src/release_gate/gate/`, gate v0.2.0), policy, triage schema, Markdown report, `gate decide` CLI, three example reports in `docs/examples/`, and README. Tasks T001–T007 are done. Claude reviewed all seven on 2026-09-27; the earlier "reviewed" status on T004–T006 had been set by the implementing model, not by Claude. Review fixes: consistent units in the rule table (engine v0.2.0), CLI crashes now exit 4 instead of 1, `.gitignore` keeps `runs/demo/`. 88 tests pass, ruff is clean.
- Sandbox note: in Claude's sandbox, pytest can't use the system temp folder. Run tests with `-p no:cacheprovider --basetemp=<scratchpad>/pytest`. On the user's machine plain `pytest -q` works.
- 2026-09-27: The user has Google Colab compute units with an A100. The user confirmed the setup and it is now in `plan.md` §9 "Execution environments" and §18: vLLM on Colab, runner inside the notebook, local mock endpoint for development and CI, CPU-only Kubernetes demo. The user pays per connected hour, so never propose workflows that keep Colab idle or need the GPU for development.
- 2026-09-27: The user chose the demo models: baseline `meta-llama/Llama-3.1-8B-Instruct` and candidate `Qwen/Qwen3-8B`. The user wants models to stay editable, so they are chosen through YAML specs in `models/` (`src/release_gate/registry.py`). Qwen3 must set `enable_thinking` explicitly. Llama is gated and needs Colab secret `HF_TOKEN`. The user asked for lower precision, so the specs use FP8 weights (renamed with an `-fp8` suffix); the pair (~19 GB) now fits concurrently on a 40 GB A100. T008 (`gate models` CLI) and T009 (`models/README.md`) are done and reviewed by Claude. 128 tests pass.
- 2026-09-27: Git remote is `origin` = https://github.com/abhishek-paul-d/LLM_GATE.git, default branch `main`. Phase 0 was pushed as commit 5a42156.
- 2026-09-27: The implementing model twice set delegated tasks to `reviewed` itself. The shared conventions now forbid it. Always verify review status yourself; don't trust it.
- 2026-09-27: Phase 1 started. Claude built the generator (`src/release_gate/generator/`: rng, names, severity, schema, scenario, variants, build, validate), 3 reference families (memory_pressure, bad_config_rollout, upstream_dependency), the `starter-v1` suite (30 cases, 15 dev / 15 release, all candidate, not yet human-reviewed), and generator tests (155 total pass). Queued: T010 (`gate suite` CLI) and T011-T016 (six families). Next for Claude after those land: review them, then design the full release-benchmark config (enough release-split cases per protected slice), then Phase 2 (runner + mock endpoint).
