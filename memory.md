# Project memory

Notes for Claude Code for this project only. Keep entries short, date them, and update or remove entries that stop being true.

## User preferences

- **Delegate simple tasks** (2026-09-27): Only do complex work. When a simpler model could do a task reliably, don't implement it. Add an entry to `delegated_tasks.json` with the target file and a self-contained prompt, then tell the user it was queued. The user runs a cheaper model for simple work to save cost. The criteria are in `CLAUDE.md` under "Task delegation".
- **Memory location** (2026-09-27): Store memories for this project in this file only. Do not write to the global Claude memory directory.

## Project state

- 2026-09-27: Phase 0 is complete except choosing the model family. Present: gate contract and engine (`src/release_gate/gate/`, gate v0.2.0), policy, triage schema, Markdown report, `gate decide` CLI, three example reports in `docs/examples/`, and README. Tasks T001–T007 are done. Claude reviewed all seven on 2026-09-27; the earlier "reviewed" status on T004–T006 had been set by the implementing model, not by Claude. Review fixes: consistent units in the rule table (engine v0.2.0), CLI crashes now exit 4 instead of 1, `.gitignore` keeps `runs/demo/`. 88 tests pass, ruff is clean.
- Sandbox note: in Claude's sandbox, pytest can't use the system temp folder. Run tests with `-p no:cacheprovider --basetemp=<scratchpad>/pytest`. On the user's machine plain `pytest -q` works.
- 2026-09-27: The user has Google Colab compute units with an A100. The user confirmed the setup and it is now in `plan.md` §9 "Execution environments" and §18: vLLM on Colab, runner inside the notebook, local mock endpoint for development and CI, CPU-only Kubernetes demo. The user pays per connected hour, so never propose workflows that keep Colab idle or need the GPU for development.
- Still open: which 3–8B model family to use as the demo baseline (`plan.md` §18). Needed before Phase 2. The repo is not under git yet.
