# SPEC.md — TTNN Model Bringup Automation (Yieldthought)

This document is the **single source of truth** for implementing an automated workflow that:
1) tracks “model bringup” work as GitHub Issues in `yieldthought/ttnn_models`,
2) uses a GitHub Project board (Projects v2) as the state machine,
3) runs workers on TT machines (`n150`, `n300`, `lb`) that claim issues, bring models up, run accuracy/perf checks, open PRs, and advance the issue state,
4) uses `gh` CLI for almost all GitHub orchestration,
5) uses `codexapi.Task` (checker-driven retries) for code-writing / iteration.

> **Two repos**
> - `yieldthought/agents` (this automation code; runs on your machines)
> - `yieldthought/ttnn_models` (model implementations + eval harness + tests; issues live here)

---

## 0) Goals and constraints

### Goals
- Fully automate the bringup loop:
  - find next eligible issue for this system type,
  - claim it safely with other workers running,
  - set up clean workdir,
  - download weights from Hugging Face,
  - implement/modify model code in `ttnn_models`,
  - run evaluation in HF-only reference mode + TT mode (trace off/on),
  - commit and push a branch,
  - open a PR with an auto-generated description,
  - move the issue through the Project statuses.

- Make the system “boring”:
  - predictable state machine,
  - clean logs and failure reporting,
  - minimal abstractions and minimal typing.

### Non-goals (for v1)
- No dynamic scheduling beyond “oldest ready first”.
- No advanced distributed locking service; concurrency handled via comments lease.
- No attempt to auto-tune kernel configs; functional bringup first, performance later.
- No background services beyond a simple “while True” worker loop.

### Hard constraints
- **Everything GitHub-related is orchestrated via `gh`** (including `gh api graphql`).
- **Workers must always clean up temp dirs**.
- **Setup failures must not be misclassified as model failures**.
- **Tracing constraints**: decode tracing requires stable shapes and no allocate/deallocate inside trace.

---

## 1) Glossary

- **System type**: one of `n150`, `n300`, `lb`.
- **Project Status**: a single-select field in Projects v2 with options:
  - `assessing`, `ready`, `in progress`, `setup error`, `failed`, `in review`, `done`, `reference error`.
- **Issue**: a GitHub Issue in `yieldthought/ttnn_models` representing one model bringup.
- **Worker**: a running process on a machine that claims and completes issues.
- **Lease / claim**: a comment-based “lock” used to prevent two workers from taking the same issue.
- **Bringup**: implement TTNN model code + run eval and meet thresholds.

---

## 2) Repo responsibilities

### 2.1 `yieldthought/agents`
Contains:
- Worker loop
- GitHub orchestration (via `gh`)
- `FunctionalBringupTask` implementation (codexapi Task subclass)
- Optional: `PRTask`, `AddModelTask`
- Machine bootstrap script (install + auth + sanity checks)

### 2.2 `yieldthought/ttnn_models`
Contains:
- Model code (TTNN implementations)
- `scripts/run_eval.py` (the checker entrypoint)
- Tests (at least smoke)
- Issue templates for model bringup + discovery

---

## 3) GitHub configuration (one-time bootstrap)

> This section is “operations”, but workers assume it exists.

### 3.1 Project board
Create a Project (Projects v2) owned by `yieldthought` named e.g. `TTNN Models Bringup`.

Create/ensure a **single-select** field named **`Status`** with options:
- assessing
- ready
- in progress
- setup error
- failed
- in review
- done
- reference error

The board view should group by `Status`.

### 3.2 Labels (routing)
In `yieldthought/ttnn_models`, create labels:
- `n150`
- `n300`
- `lb`

Workers filter issues by label matching their system type.

### 3.3 Issue templates (required)
Add two issue templates under `.github/ISSUE_TEMPLATE/` in `ttnn_models`.

#### Template A: `bringup_model.yml`
Required fields in issue body (machine-parsable):
- `hf_model_id: <org/name>`
- `hf_revision: <optional>`
- `architecture: <optional>`
- `notes: <optional>`
- `prefill_len: <optional int>`
- `decode_len: <optional int>`
- `batch: <optional int>`

Also require label: one of `n150`/`n300`/`lb`.

#### Template B: `new_model_candidate.yml`
Required fields:
- `hf_model_id`
- `hf_revision`
- `notes`

Used by `AddModelTask`.

---

## 4) Auth and secrets

### 4.1 Required CLI tools on worker machines
- `gh` (GitHub CLI)
- `git`
- `python3`
- `codex` CLI (used indirectly by `codexapi`)
- `tt-smi` (or equivalent TT reset tool)
- (optional) `jq` (recommended for parsing; otherwise parse JSON in Python)

### 4.2 Required environment variables
On worker machines:

**Routing / identity**
- `YT_SYSTEM` = `n150` | `n300` | `lb`
- `YT_OWNER` = `yieldthought`
- `YT_REPO_MODELS` = `ttnn_models`
- `YT_WORKER_NAME` = hostname or a custom identifier (default: hostname)

**GitHub Project**
- `YT_PROJECT_NUMBER` = numeric project number (preferred), OR allow discovery by title.
- `YT_PROJECT_TITLE` (optional) if using title discovery

**Hugging Face**
- `HF_TOKEN` must be present for gated models (or rely on existing HF auth)

**Codex**
- `CODEX_BIN` optional (defaults to `codex`)
- `CODEX_*` auth env vars as required by your codex installation

**Policy / thresholds**
- `YT_TOP1_MIN` default 0.90 (example; tune per model)
- `YT_TOP5_MIN` default 0.97
- `YT_MAX_ATTEMPTS` default 10
- `YT_SLEEP_SECS` default 20
- `YT_TMP_ROOT` optional (default: system tmp)

### 4.3 GitHub CLI auth scopes
Workers require:
- `repo` access
- `project` scope for Projects v2 operations

Bootstrap should ensure:
- `gh auth status` is OK
- If missing project scope: `gh auth refresh -s project`

---

## 5) State machine (Project Status)

| Status          | Meaning                                                      | Who sets it |
|----------------|--------------------------------------------------------------|-------------|
| assessing       | candidate created; not validated                              | AddModelTask or human |
| reference error | HF-only validation failed                                     | AddModelTask / worker |
| ready           | validated HF-only; queued for bringup                         | AddModelTask / human |
| in progress     | claimed by a worker and actively running                      | worker |
| setup error     | infrastructure failure: missing tools, reset failure, etc.    | worker |
| failed          | code ran but accuracy/perf checks couldn’t be met             | worker |
| in review       | PR opened and awaiting tests/review/merge                     | worker |
| done            | merged to main                                                | PRTask / human |

---

## 6) Claim/lease protocol (concurrency)

### 6.1 Claim comment format
Workers claim by posting a comment containing a unique marker:

```
[yt-claim]
worker: <YT_WORKER_NAME>
system: <YT_SYSTEM>
run_id: 
timestamp: 
```

### 6.2 Claim algorithm (must implement exactly)
For each eligible issue candidate:
1) Post claim comment:
   - `gh issue comment <ISSUE_NUMBER> -R yieldthought/ttnn_models --body "<comment>"`
2) Verify claim is **the latest claim**:
   - Fetch the most recent comments (prefer GraphQL `last: 10`) and find the last comment containing `[yt-claim]`.
   - If the last `[yt-claim]` is authored by this worker and has the same `run_id`, claim succeeds.
3) If claim fails:
   - Attempt to delete last comment by this user (best effort):
     - `gh issue comment <ISSUE_NUMBER> -R yieldthought/ttnn_models --delete-last --yes`
   - Move on to next candidate.

### 6.3 Lease timeout (optional v1)
To prevent stuck claims, you MAY ignore claims older than `YT_CLAIM_TTL_SECS` (default 3600). This is optional for v1.

---

## 7) GitHub orchestration requirements (use `gh`)

### 7.1 Required operations
Workers must implement via `gh`:

- List eligible issues (Project Status + label + open)
- Post and delete comments
- Move Project Status
- Clone repo and push branch
- Create PR
- Comment PR link and results back onto issue

### 7.2 Recommended: `gh api graphql` for Projects v2
Projects v2 needs IDs: `projectId`, `fieldId`, `optionId`, `itemId`.

Workers must implement a “project cache” on startup:
- resolve project ID from `YT_PROJECT_NUMBER` (preferred) or title,
- resolve Status field ID and option IDs for each status string,
- store mapping in memory.

#### Required GraphQL mutations/queries (exact intentions)

> You do not have to use these exact query strings, but must provide equivalent functionality.

**A) Resolve project and Status option IDs**
- query project by owner + number OR title
- retrieve fields, find field named `Status`
- for that field, retrieve option IDs for each option name

**B) Given an issue, find the Project item ID**
- query issue by number
- list projectItems for that issue, find the one in your project

**C) Update project status**
- mutation `updateProjectV2ItemFieldValue` setting the single-select option

### 7.3 Minimal `gh` command patterns (examples)
- Comment:
  - `gh issue comment <n> -R yieldthought/ttnn_models --body "..."`

- View issue as JSON:
  - `gh issue view <n> -R yieldthought/ttnn_models --json title,labels,body,comments`

- Create PR:
  - `gh pr create -R yieldthought/ttnn_models --base main --head <branch> --title "..." --body "..."`

- Merge PR (PRTask):
  - `gh pr merge -R yieldthought/ttnn_models <pr_number> --squash --delete-branch --auto`

---

## 8) Workspace rules

### 8.1 Tempdir lifecycle
For each issue run:
- Create a tempdir under `YT_TMP_ROOT` or system tmp
- All work (clone, downloads, artifacts) happens inside that tempdir
- Tempdir is deleted **no matter what** (finally block)

### 8.2 Repo cloning
Inside tempdir:
- `git clone git@github.com:yieldthought/ttnn_models.git`
- `git checkout -b <branch>`

### 8.3 Branch naming
Branch name format:

`bringup/issue-<ISSUE_NUMBER>-<SANITIZED_HF_ID>`

Where `SANITIZED_HF_ID`:
- lowercased
- `/` -> `-`
- remove characters not `[a-z0-9._-]` (replace with `-`)
- collapse repeats of `-`

Example:
- `meta-llama/Llama-3.1-8B-Instruct` -> `meta-llama-llama-3.1-8b-instruct`

### 8.4 Branch existence check
Before creating branch, ensure it doesn’t already exist remotely:
- `git ls-remote --heads origin <branch>`
If it exists:
- raise a “setup error” (to avoid collisions)

---

## 9) Task definitions in `yieldthought/agents`

Implementation must use `codexapi.Task` (checker-driven retries) for bringup.

### 9.1 FunctionalBringupTask (required)

#### Inputs (from issue)
- issue number
- hf_model_id (+ revision)
- system label (`YT_SYSTEM`)
- optional eval params (prefill/decode/batch)

#### Steps (must follow in order)

**A) Select + claim issue**
- list eligible issues:
  - status == `ready`
  - has label `YT_SYSTEM`
  - open
- claim using lease protocol
- on claim success: move status -> `in progress`

**B) Setup**
- create tempdir
- clone `ttnn_models`
- create branch
- verify prerequisites:
  - `python -c "import ttnn"` (or equivalent)
  - `tt-smi` exists and responds
  - `gh auth status` passes
- if any fails -> status `setup error`, comment details, abort

**C) Download HF weights**
- use `huggingface_hub` or `huggingface-cli download`
- must respect `HF_TOKEN`
- if fails -> status `setup error`, comment details, abort

**D) Reset TT hardware**
- run your reset command (e.g. `tt-smi reset` or equivalent)
- if fails -> status `setup error`, comment details, abort

**E) Run Codex “bringup prompt”**
- The codex task prompt must instruct:
  - implement bringup for this model under `ttnn_models`
  - ensure eval harness works
  - use BFP8 weights
  - prefer SDPA ops and TT fused RoPE when available
  - ensure prefill vs decode conventions are respected (prefill in DRAM interleaved, decode in L1 sharded, tracing only for decode)
  - do not touch outside this repo

**F) Checker loop**
In `check()` run (from repo root):
1) `python -m pytest -q` (fast gate; can be small test set in v1)
2) HF-only eval:
   - `python scripts/run_eval.py --mode hf --hf-model <id> [--revision ...] --prefill-len ... --decode-len ... --batch ...`
3) TT eval, trace off:
   - `python scripts/run_eval.py --mode tt --trace 0 ...`
4) TT eval, trace on:
   - `python scripts/run_eval.py --mode tt --trace 1 ...`

If any command non-zero or metrics below thresholds -> return a detailed error string including:
- failing command
- stdout/stderr tail (last N lines)
- parsed metrics JSON if available

**G) Success hook**
On success:
- run eval again to obtain final metrics JSON
- `git status` must be clean except intended files
- commit message format:
  - `Bringup <hf_model_id> (<YT_SYSTEM>) top1=<x> top5=<y> trace=<0/1>`

- push branch:
  - `git push -u origin <branch>`

- create PR:
  - title: `Bringup: <hf_model_id> (<YT_SYSTEM>)`
  - body must include:
    - issue reference (`Closes #<issue>`)
    - how to run eval command(s)
    - metrics JSON block
    - any known limitations

- comment on issue with:
  - PR link
  - metrics summary
  - how to reproduce

- move status -> `in review`

**H) Failure hook**
There are two failure classes:

1) **Setup failure** (in setup steps before Codex loop can reasonably proceed):
   - move status -> `setup error`
   - comment exception and what failed (with logs)
   - do NOT open PR unless you intentionally want a “fix infra” PR

2) **Bringup failure** (Codex loop ran but checks couldn’t pass within attempts):
   - move status -> `failed`
   - commit and push the branch (so work is preserved)
   - comment summary and reproduction steps

**I) Always tear down**
- delete tempdir

---

### 9.2 PRTask (optional but recommended)

Eligible issues:
- status == `in review`

Workflow:
1) Find linked PR (by searching PRs referencing the issue number or querying issue timeline).
2) Rebase PR branch on latest main:
   - `git fetch origin main`
   - `git rebase origin/main`
   - push with `--force-with-lease`
3) Run tests (at minimum `pytest -q`; optionally eval smoke)
4) If green:
   - merge PR via `gh pr merge --auto --squash --delete-branch`
   - move issue status -> `done`
5) If red:
   - comment failure summary
   - move issue status -> `failed` (or keep `in review` if you prefer manual triage)

---

### 9.3 AddModelTask (optional)

Runs periodically (e.g. once every N hours), does:
1) Query Hugging Face for popular models
2) Filter by size constraints:
   - n150: <= 8B
   - n300: <= 20B
   - lb: <= 70B
3) Skip models already having an issue in the Project (match by `hf_model_id`)
4) Create issue with status `assessing`
5) Run HF-only eval (`scripts/run_eval.py --mode hf ...`)
6) If pass: move status -> `ready`
7) If fail: move status -> `reference error`

---

## 10) `ttnn_models` eval harness contract (required)

### 10.1 Single entrypoint: `scripts/run_eval.py`
Must accept:
- `--hf-model <id>` (required)
- `--revision <rev>` (optional)
- `--mode hf|tt` (required)
- `--trace 0|1` (optional; only valid for `--mode tt`)
- `--prefill-len <int>` (default: 128)
- `--decode-len <int>` (default: 32)
- `--batch <int>` (default: 1)
- `--seed <int>` (default: 0)

Behavior:
- Exit code 0 on success
- Non-zero exit code on error
- Must print a final JSON blob (single line) to stdout prefixed clearly, e.g.:
  - `YT_METRICS=<json>`

Required metrics keys:
- `top1` (float 0..1)
- `top5` (float 0..1)
- `tokens` (int)
- `mode` ("hf" or "tt")
- `trace` (bool)
- `timing` object (at least one timing measurement; can be coarse)

### 10.2 Teacher forcing accuracy definition
For each decode step:
- Reference logits = HF CPU model
- TT logits = TT model
Compute:
- Top-1 agreement: `argmax(tt_logits) == argmax(hf_logits)`
- Top-5 agreement: `argmax(hf_logits) in top5(tt_logits)`

Aggregate across steps and report as fractions.

### 10.3 Tracing requirements
When `--mode tt --trace 1`:
- only trace decode path (recommended)
- ensure stable shapes
- no allocate/deallocate inside trace region

### 10.4 Minimum tests
Add at least one pytest that:
- runs HF-only mode on a tiny model or mocked config
- verifies `YT_METRICS=` JSON appears and parses
- does NOT require TT hardware

---

## 11) Machine bootstrap script requirements (`yieldthought/agents/scripts/bootstrap_machine.sh`)

The script must:
1) Install or validate presence of:
   - python deps (including `codexapi`)
   - `gh`
   - `git`
   - `codex`
   - `tt-smi`
2) Validate auth:
   - `gh auth status`
   - ensure project scope (print actionable instructions if missing)
3) Validate TT Python package:
   - `python -c "import ttnn; print(ttnn.__version__)"` (or just import)
4) Validate HF token presence:
   - if `HF_TOKEN` missing, print warning (not fatal for ungated models)

Script must be safe to re-run (idempotent checks).

---

## 12) Logging, artifacts, and reporting

### 12.1 Local logs
Workers must log:
- issue number, run_id, worker name
- state transitions
- command invocations (at least at INFO)
- stdout/stderr tails on failure

### 12.2 Issue comments (minimum)
On any terminal outcome (setup error / failed / in review):
- add a comment containing:
  - status set
  - run_id
  - short summary
  - reproduction commands

### 12.3 Preserve work product on bringup failure
If bringup fails after code changes:
- commit + push branch (so humans can inspect)
- link branch name in issue comment

---

## 13) Acceptance tests (what “done” means)

### 13.1 `yieldthought/agents` acceptance (local, no GitHub)
Provide a `--dry-run` mode that:
- does not call `gh` and does not reset TT
- prints what it WOULD do (selected issue, commands)

Unit tests (pytest) must cover:
- branch name sanitization
- parsing issue body fields
- metrics JSON parsing from `YT_METRICS=...`
- classification: setup error vs bringup failure

### 13.2 `yieldthought/agents` acceptance (integration, requires GitHub)
Given a real project and one test issue in `ready`:
- worker claims it (comment lease)
- moves status to `in progress`
- clones repo and creates branch
- runs at least HF-only eval (TT eval may be skipped on non-TT machine)
- on success path:
  - opens PR
  - moves issue to `in review`

### 13.3 `yieldthought/ttnn_models` acceptance (local)
- `python scripts/run_eval.py --mode hf --hf-model <some tiny model> --prefill-len 8 --decode-len 4 --batch 1`
  - exits 0
  - prints `YT_METRICS=...` parseable JSON
- `pytest -q` passes without TT hardware

---

## 14) Implementation style rules (must follow)
- Prefer simple modules and clear docstrings over frameworks.
- Avoid heavy typing / generics; keep interfaces minimal.
- Fail loudly and clearly on external command errors.
- Keep the worker loop small and readable.
- Do not attempt to “handle” every possible broken state; define the contract and enforce it.

---

## 15) File/Module deliverables checklist

### 15.1 `yieldthought/agents` (required files)
- `scripts/run_worker.py` (entrypoint)
- `scripts/bootstrap_machine.sh`
- `src/yieldthought_agents/worker.py`
- `src/yieldthought_agents/github.py`
- `src/yieldthought_agents/shell.py`
- `src/yieldthought_agents/tasks/functional_bringup.py`
- `README.md` with env var docs + examples

### 15.2 `yieldthought/ttnn_models` (required files)
- `scripts/run_eval.py`
- `tests/test_eval_smoke.py`
- `.github/ISSUE_TEMPLATE/bringup_model.yml`
- `.github/ISSUE_TEMPLATE/new_model_candidate.yml`
- `README.md` with how to run eval

---

## 16) Example end-to-end operator flow (for humans)

1) Create issue using template “Bring up model”, label it `n150`, set Status to `ready`.
2) On an n150 machine:
   - export env vars (`YT_SYSTEM=n150`, tokens, project number)
   - run `python scripts/run_worker.py`
3) Worker:
   - claims issue
   - status -> in progress
   - does bringup and checks
   - opens PR
   - status -> in review
4) PRTask merges and sets done (or human merges)

---

END OF SPEC
