"""Functional bringup task for TTNN models."""

import json
import logging
import os
import re
import tempfile

from codexapi import Task

from ..shell import tail_lines


class SetupError(RuntimeError):
    """Raised when setup steps fail before bringup begins."""


def sanitize_branch_name(hf_model_id):
    """Normalize HF model ids for branch names."""
    value = hf_model_id.lower().replace("/", "-")
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def parse_metrics(output):
    """Parse YT_METRICS JSON from output."""
    for line in output.splitlines():
        if line.startswith("YT_METRICS="):
            payload = line.split("=", 1)[1].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
    return None


def _keep_tmp_enabled():
    value = os.environ.get("YT_KEEP_TMP", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _local_mode_enabled():
    value = os.environ.get("YT_LOCAL_MODE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class FunctionalBringupTask(Task):
    """Bring up a model with checker-driven retries."""

    def __init__(
        self,
        branch,
        hf_model_id,
        system,
        top1_min,
        top5_min,
        max_attempts,
        owner,
        repo,
        tmp_root,
        prefill_len,
        decode_len,
        batch,
        shell,
        keep_tmp=None,
        logger=None,
    ):
        self.branch = branch
        self.hf_model_id = hf_model_id
        self.system = system
        self.top1_min = top1_min
        self.top5_min = top5_min
        self.owner = owner
        self.repo = repo
        self.tmp_root = tmp_root
        self.prefill_len = prefill_len
        self.decode_len = decode_len
        self.batch = batch
        self.shell = shell
        if keep_tmp is None:
            keep_tmp = _keep_tmp_enabled()
        self.keep_tmp = keep_tmp
        self.logger = logger or logging.getLogger(__name__)
        self.repo_root = None
        self.tmp_dir = None
        self.metrics = {}
        self.final_metrics = None
        self.did_commit = False
        self.commit_sha = None

        prompt = _build_prompt(hf_model_id, system, top1_min, top5_min)
        codex_flags = os.environ.get(
            "YT_CODEX_FLAGS",
            "--dangerously-bypass-approvals-and-sandbox -c model_reasoning_effort=\"low\"",
        )
        full_auto = True
        if codex_flags and "dangerously-bypass-approvals-and-sandbox" in codex_flags:
            full_auto = False
        super().__init__(
            prompt,
            max_attempts=max_attempts,
            cwd=None,
            flags=codex_flags or None,
            full_auto=full_auto,
        )

    def set_up(self):
        """Prepare workspace, download weights, and reset hardware."""
        self.tmp_dir = tempfile.mkdtemp(dir=self.tmp_root)
        clone_dir = os.path.join(self.tmp_dir, self.repo)
        repo_url = f"git@github.com:{self.owner}/{self.repo}.git"
        self.shell.run(["git", "clone", repo_url], cwd=self.tmp_dir)
        self.repo_root = clone_dir
        self.cwd = self.repo_root
        self.agent.cwd = self.repo_root

        if not self.branch:
            raise SetupError("Missing branch name for bringup task")
        remote = self.shell.run(
            ["git", "ls-remote", "--heads", "origin", self.branch],
            cwd=self.repo_root,
        )
        if remote.stdout.strip():
            raise SetupError(f"Branch already exists: {self.branch}")

        self.shell.run(["git", "checkout", "-b", self.branch], cwd=self.repo_root)
        self._check_prereqs()
        self._download_weights()
        self._reset_hardware()

    def tear_down(self):
        """Clean up the temp directory."""
        if self.tmp_dir:
            self.logger.info("Keeping temp repo at %s", self.tmp_dir)
        return

    def check(self):
        """Run tests and evals; return an error string if any fail."""
        self.metrics = {}
        steps = [
            ("pytest", ["python", "-m", "pytest", "-q"], False),
            ("hf", self._eval_command("hf", None), True),
            ("tt-trace-off", self._eval_command("tt", 0), True),
            ("tt-trace-on", self._eval_command("tt", 1), True),
        ]
        errors = []
        for name, cmd, expect_metrics in steps:
            result = self.shell.run(cmd, cwd=self.repo_root, check=False)
            if result.returncode != 0:
                error = _format_failure(cmd, result.stdout, result.stderr, None)
                self.logger.error("Check failed: %s", error)
                errors.append(error)
                continue
            metrics = None
            if expect_metrics:
                metrics = parse_metrics(result.stdout)
                if not metrics:
                    error = _format_failure(cmd, result.stdout, result.stderr, None)
                    self.logger.error("Check failed: %s", error)
                    errors.append(error)
                    continue
                self.metrics[name] = metrics
                if not _metrics_ok(metrics, self.top1_min, self.top5_min):
                    error = _format_failure(cmd, result.stdout, result.stderr, metrics)
                    self.logger.error("Check failed: %s", error)
                    errors.append(error)

        agent_error = self._run_agent_check()
        if agent_error:
            errors.append(agent_error)

        if errors:
            return "\n\n".join(errors)
        return None

    def on_success(self, result):
        """Finalize local changes on success."""
        metrics = self._run_final_eval()
        self.final_metrics = metrics
        self._commit_work(metrics, success=True)

    def on_failure(self, result):
        """Persist local changes after bringup failure."""
        self._commit_work(None, success=False)

    def _check_prereqs(self):
        self.shell.run(["python", "-c", "import ttnn"], cwd=self.repo_root)
        self.shell.run(["tt-smi", "--help"], cwd=self.repo_root)
        if _local_mode_enabled():
            self.logger.info("YT_LOCAL_MODE enabled; skipping gh auth check")
            return
        self.shell.run(["gh", "auth", "status"], cwd=self.repo_root)

    def _download_weights(self):
        script = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download(repo_id={self.hf_model_id!r})"
        )
        self.shell.run(["python", "-c", script], cwd=self.repo_root)

    def _reset_hardware(self):
        self.shell.run(["tt-smi", "-r"], cwd=self.repo_root)

    def _eval_command(self, mode, trace):
        cmd = [
            "python",
            "scripts/run_eval.py",
            "--mode",
            mode,
            "--hf-model",
            self.hf_model_id,
            "--system",
            self.system,
        ]
        if self.prefill_len is not None:
            cmd.extend(["--prefill-len", str(self.prefill_len)])
        if self.decode_len is not None:
            cmd.extend(["--decode-len", str(self.decode_len)])
        if self.batch is not None:
            cmd.extend(["--batch", str(self.batch)])
        if trace is not None:
            cmd.extend(["--trace", str(trace)])
        return cmd

    def _run_final_eval(self):
        cmd = self._eval_command("tt", 1)
        result = self.shell.run(cmd, cwd=self.repo_root, check=False)
        if result.returncode != 0:
            raise RuntimeError(_format_failure(cmd, result.stdout, result.stderr, None))
        metrics = parse_metrics(result.stdout)
        if not metrics:
            raise RuntimeError("Final eval missing metrics JSON")
        return metrics

    def _agent_check_prompt(self):
        return (
            "Review the bringup implementation in this repo for the model "
            f"{self.hf_model_id}. Focus on tensor shapes, padded shapes, QKV "
            "splits, RoPE format, cache usage, and TTNN op constraints. "
            "Follow the guidance in doc/ttnn.md and the task prompt, and "
            "ensure the code matches the spirit of the bringup flow. "
            "Do not edit files; just review.\n\n"
            "Output format:\n"
            "CHECK=PASS\n"
            "or\n"
            "CHECK=FAIL\n"
            "then a short list of issues with file paths and line numbers."
        )

    def _run_agent_check(self):
        try:
            output = self.agent(self._agent_check_prompt())
        except Exception as exc:
            return f"Agent check error: {exc}"
        ok, status = _parse_agent_check(output)
        if ok:
            return None
        return "Agent check failed:\n" + status

    def _commit_work(self, metrics, success):
        status = self.shell.run(["git", "status", "--porcelain"], cwd=self.repo_root)
        if not status.stdout.strip():
            if success:
                raise RuntimeError("No changes to commit after successful checks")
            self.logger.info("No changes to commit")
            return
        self.shell.run(["git", "add", "-A"], cwd=self.repo_root)
        if success:
            trace = 1 if metrics and metrics.get("trace") else 0
            top1 = _format_metric(metrics, "top1")
            top5 = _format_metric(metrics, "top5")
            message = f"Bringup {self.hf_model_id} ({self.system}) top1={top1} top5={top5} trace={trace}"
        else:
            message = f"WIP Bringup {self.hf_model_id} ({self.system})"
        self.shell.run(["git", "commit", "-m", message], cwd=self.repo_root)
        self.did_commit = True
        self.commit_sha = self.shell.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_root,
        ).stdout.strip()
        clean = self.shell.run(["git", "status", "--porcelain"], cwd=self.repo_root)
        if clean.stdout.strip():
            raise RuntimeError("Working tree not clean after commit")

    def repro_commands(self):
        """Return commands for reproducing evals."""
        commands = [
            _format_cmd(self._eval_command("hf", None)),
            _format_cmd(self._eval_command("tt", 0)),
            _format_cmd(self._eval_command("tt", 1)),
        ]
        return "\n".join(commands)


def _build_prompt(hf_model_id, system, top1_min, top5_min):
    template = _load_prompt_template()
    base = _render_prompt(template, hf_model_id, system, top1_min, top5_min)
    return base


def _load_prompt_template():
    """Load the base bringup prompt template."""
    path = os.path.join(os.path.dirname(__file__), "functional_bringup.txt")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _render_prompt(template, hf_model_id, system, top1_min, top5_min):
    """Render the prompt template placeholders."""
    replacements = {
        "{HF_MODEL}": hf_model_id,
        "{SYSTEM}": system,
        "{TOP_1_TARGET}": str(top1_min),
        "{TOP_5_TARGET}": str(top5_min),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def _metrics_ok(metrics, top1_min, top5_min):
    return metrics.get("top1", 0) >= top1_min and metrics.get("top5", 0) >= top5_min


def _format_failure(cmd, stdout, stderr, metrics):
    command = _format_cmd(cmd)
    summary = [f"Command failed: {command}"]
    if metrics:
        summary.append(f"metrics: {json.dumps(metrics)}")
    if stdout:
        summary.append("stdout tail:\n" + tail_lines(stdout))
    if stderr:
        summary.append("stderr tail:\n" + tail_lines(stderr))
    return "\n".join(summary)


def _format_cmd(cmd):
    if isinstance(cmd, str):
        return cmd
    return " ".join(str(part) for part in cmd)


def _metrics_summary(metrics):
    top1 = _format_metric(metrics, "top1")
    top5 = _format_metric(metrics, "top5")
    trace = 1 if metrics.get("trace") else 0
    return f"top1={top1} top5={top5} trace={trace}"


def _format_metric(metrics, key):
    value = metrics.get(key)
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def _parse_agent_check(output):
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("check="):
            status = stripped.split("=", 1)[1].strip().lower()
            if status == "pass":
                return True, output
            if status == "fail":
                return False, output
            return False, f"Agent check returned unknown status: {stripped}\n{output}"
    return False, "Agent check missing CHECK=PASS/FAIL line.\n" + output
