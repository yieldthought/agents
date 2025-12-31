"""Worker loop for TTNN model bringup automation."""

import argparse
import datetime
import json
import logging
import os
import re
import socket
import time
import uuid

from .github import GitHubClient
from .shell import Shell
from .tasks.functional_bringup import FunctionalBringupTask, SetupError, sanitize_branch_name


def parse_issue_body(body):
    """Parse key-value fields from an issue body."""
    fields = {}
    if not body:
        return fields
    for line in body.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value in ("<optional>", "<optional int>", "<optional>"):
            value = ""
        fields[key] = value
    return fields


def classify_outcome(result, error):
    """Classify an outcome as setup error, bringup failure, or success."""
    if error:
        return "setup error"
    if result and not result.success:
        return "failed"
    return "success"


def _int_or_none(value):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return int(value)


class Worker:
    """Worker loop that claims and runs bringup tasks."""

    def __init__(self, dry_run=False, dry_run_issue=None):
        self.dry_run = dry_run
        self.dry_run_issue = dry_run_issue
        self.logger = logging.getLogger("yieldthought.worker")
        self.shell = Shell(logger=self.logger)
        env_system = os.environ.get("YT_SYSTEM")
        self.system = env_system or detect_system(self.shell)
        if not env_system:
            self.logger.info("Detected system: %s", self.system)
        self.owner = os.environ.get("YT_OWNER", "yieldthought")
        self.repo = os.environ.get("YT_REPO_MODELS", "ttnn_models")
        self.worker_name = os.environ.get("YT_WORKER_NAME", socket.gethostname())
        self.project_number = _env_int("YT_PROJECT_NUMBER", 2)
        self.project_title = os.environ.get("YT_PROJECT_TITLE")
        self.top1_min = _env_float("YT_TOP1_MIN", 0.90)
        self.top5_min = _env_float("YT_TOP5_MIN", 0.97)
        self.max_attempts = _env_int("YT_MAX_ATTEMPTS", 10)
        self.sleep_secs = _env_int("YT_SLEEP_SECS", 20)
        self.tmp_root = os.environ.get("YT_TMP_ROOT")
        self.github = GitHubClient(
            self.owner,
            self.repo,
            self.project_number,
            self.project_title,
            shell=self.shell,
            logger=self.logger,
        )

    def run_once(self):
        """Run a single claim + bringup cycle if work is available."""
        if self.dry_run:
            self.describe_dry_run()
            return False

        ready = self.github.list_ready_issues(self.system)
        if not ready:
            self.logger.info("No ready issues for %s", self.system)
            return False

        for issue in ready:
            number = issue["number"]
            run_id = uuid.uuid4().hex
            if not self._claim_issue(number, run_id):
                continue
            self.logger.info(
                "Claimed issue %s (run_id=%s worker=%s)",
                number,
                run_id,
                self.worker_name,
            )
            try:
                self.github.move_issue_status(number, "in progress")
                self.logger.info("Status -> in progress for issue %s", number)
                task, result = self._run_task(number)
                if not result:
                    raise RuntimeError("Bringup task returned no result")
                if result.success:
                    self._handle_success(number, run_id, task)
                else:
                    self.logger.info("Bringup failed for issue %s (run_id=%s)", number, run_id)
                    self._handle_failure(number, run_id, task, result)
                return True
            except Exception as exc:
                self.logger.exception("Issue %s setup error (run_id=%s)", number, run_id)
                self._handle_setup_error(number, run_id, exc)
                return True
        return False

    def _claim_issue(self, number, run_id):
        claim = _claim_comment(self.worker_name, self.system, run_id)
        self.github.comment_issue(number, claim)
        first_claim = self.github.get_first_claim(number)
        if not first_claim:
            return False
        if first_claim.get("author") != self.github.viewer_login():
            return False
        if first_claim.get("run_id") != run_id:
            return False
        return True

    def _run_task(self, number):
        issue = self.github.get_issue(number)
        fields = parse_issue_body(issue.get("body") or "")
        hf_model_id = fields.get("hf_model_id")
        if not hf_model_id:
            raise SetupError("Missing hf_model_id in issue body")
        hf_revision = fields.get("hf_revision") or None
        prefill_len = _int_or_none(fields.get("prefill_len"))
        decode_len = _int_or_none(fields.get("decode_len"))
        batch = _int_or_none(fields.get("batch"))

        branch = _branch_name(number, hf_model_id)
        task = FunctionalBringupTask(
            branch,
            hf_model_id,
            hf_revision,
            self.system,
            self.top1_min,
            self.top5_min,
            self.max_attempts,
            self.owner,
            self.repo,
            self.tmp_root,
            prefill_len,
            decode_len,
            batch,
            self.shell,
            self.logger,
        )
        result = task()
        return task, result

    def _handle_setup_error(self, number, run_id, exc):
        message = (
            "[yt-status]\n"
            f"status: setup error\n"
            f"run_id: {run_id}\n"
            f"summary: {exc}\n"
            "repro: (setup failed before checks)"
        )
        self.github.move_issue_status(number, "setup error")
        self.github.comment_issue(number, message)

    def _handle_success(self, number, run_id, task):
        metrics = task.final_metrics
        if not metrics:
            raise RuntimeError("Bringup missing final metrics")
        self._push_branch(task)
        commands = task.repro_commands()
        pr_body = _pr_body(number, metrics, commands)
        pr_title = f"Bringup: {task.hf_model_id} ({self.system})"
        pr_url = self.github.create_pr(pr_title, pr_body, task.branch)
        comment = _success_comment(run_id, metrics, pr_url, commands)
        self.github.comment_issue(number, comment)
        self.github.move_issue_status(number, "in review")

    def _handle_failure(self, number, run_id, task, result):
        self._push_branch(task)
        commands = task.repro_commands()
        comment = _failure_comment(run_id, result, task.branch, commands)
        self.github.comment_issue(number, comment)
        self.github.move_issue_status(number, "failed")

    def _push_branch(self, task):
        if not task.repo_root or not task.branch:
            raise RuntimeError("Missing repo root or branch for push")
        self.shell.run(["git", "push", "-u", "origin", task.branch], cwd=task.repo_root)


def _env_int(name, default=None):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(name, default=None):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


def _claim_comment(worker, system, run_id):
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    return (
        "[yt-claim]\n"
        f"worker: {worker}\n"
        f"system: {system}\n"
        f"run_id: {run_id}\n"
        f"timestamp: {timestamp}"
    )


def _branch_name(issue_number, hf_model_id):
    sanitized = sanitize_branch_name(hf_model_id)
    return f"bringup/issue-{issue_number}-{sanitized}"


def _pr_body(issue_number, metrics, commands):
    metrics_json = json.dumps(metrics, indent=2, sort_keys=True)
    return (
        f"Closes #{issue_number}\n\n"
        "## Repro\n"
        f"{commands}\n\n"
        "## Metrics\n"
        "```json\n"
        f"{metrics_json}\n"
        "```\n\n"
        "## Known limitations\n"
        "None noted."
    )


def _success_comment(run_id, metrics, pr_url, commands):
    summary = _metrics_summary(metrics)
    return (
        "[yt-status]\n"
        "status: in review\n"
        f"run_id: {run_id}\n"
        f"summary: {summary}\n"
        f"pr: {pr_url}\n"
        "repro:\n"
        f"{commands}"
    )


def _failure_comment(run_id, result, branch, commands):
    summary = result.summary or "Bringup attempts exhausted"
    error = result.errors or ""
    return (
        "[yt-status]\n"
        "status: failed\n"
        f"run_id: {run_id}\n"
        f"summary: {summary}\n"
        f"error: {error}\n"
        f"branch: {branch}\n"
        "repro:\n"
        f"{commands}"
    )


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


def detect_system(shell):
    """Infer system type from tt-smi output."""
    result = shell.run(["tt-smi", "-ls"], check=False)
    if result.returncode != 0:
        raise RuntimeError("Failed to run tt-smi -ls to detect YT_SYSTEM")
    if not result.stdout:
        raise RuntimeError("tt-smi -ls output is empty, cannot detect system")

    text = result.stdout.lower()
    if "n150" in text:
        return "n150"
    n300_l = re.findall(r"\bn300\s+l\b", text)
    if len(n300_l) >= 4:
        return "lb"
    if n300_l:
        return "n300"
    raise RuntimeError("Unable to detect system type from tt-smi output: %s", result.stdout)


def main():
    """Worker CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Run the TTNN bringup worker")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    worker = Worker()
    while True:
        did_work = worker.run_once()
        if args.once:
            return
        if not did_work:
            time.sleep(worker.sleep_secs)


if __name__ == "__main__":
    main()
