# Automod agents

Automation for TTNN model bringup using GitHub issues/projects and codexapi tasks.

## Requirements
- `gh`, `git`, `python3`, `codex` CLI
- `tt-smi` (or equivalent TT reset tool)
- `codexapi` Python package

Install Python deps:

```bash
pip install -r requirements.txt
```

Install as a package (adds `yt-bringup-worker`):

```bash
pip install -e .
```

## Environment variables

Defaults:
- `YT_SYSTEM` auto-detected via `tt-smi -ls` (`n150`, `n300`, `lb`) unless set
- `YT_OWNER` (default: `yieldthought`)
- `YT_REPO_MODELS` (default: `ttnn_models`)
- `YT_WORKER_NAME` (default: hostname)

Project config:
- `YT_PROJECT_NUMBER` (default: `2`) or `YT_PROJECT_TITLE`

HF + Codex:
- `HF_TOKEN` (required for gated models)
- `CODEX_BIN` (optional)
- `CODEX_*` env vars required by your codex install

Policy defaults:
- `YT_TOP1_MIN` (default `0.90`)
- `YT_TOP5_MIN` (default `0.97`)
- `YT_MAX_ATTEMPTS` (default `10`)
- `YT_SLEEP_SECS` (default `20`)
- `YT_TMP_ROOT` (optional temp root)

## Run the worker

```bash
python scripts/run_worker.py
```

Run once and exit:

```bash
python scripts/run_worker.py --once
```

Dry run (prints actions only):

```bash
python scripts/run_worker.py --dry-run
```

Dry run with a specific issue number in the output:

```bash
python scripts/run_worker.py --dry-run --issue 123
```

## Bootstrap a machine

```bash
scripts/bootstrap_machine.sh
```

## Tests

```bash
pytest -q
```
