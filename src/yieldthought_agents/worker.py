"""Worker loop for TTNN model bringup automation."""

import argparse
import datetime
import logging
import os
import re
import socket
import time
import uuid

from .github import GitHubClient
from .shell import Shell
from .tasks.functional_bringup import FunctionalBringupTask, SetupError


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
                result = self._run_task(number, run_id)
                if result and not result.success:
                    self.logger.info("Bringup failed for issue %s (run_id=%s)", number, run_id)
                return True
            except Exception as exc:
                self.logger.exception("Issue %s setup error (run_id=%s)", number, run_id)
                self._handle_setup_error(number, run_id, exc)
                return True
        return False

    def describe_dry_run(self):
        """Describe the actions without calling gh or resetting hardware."""
        issue = self.dry_run_issue if self.dry_run_issue is not None else "<issue>"
        branch = f"bringup/issue-{issue}-<hf_model_id>"
        self.logger.info("Dry run: would list ready issues labeled %s", self.system)
        if self.dry_run_issue is not None:
            self.logger.info("Dry run: selected issue %s", self.dry_run_issue)
        else:
            self.logger.info("Dry run: would claim the oldest ready issue")
        self.logger.info(
            "Dry run: gh issue list -R %s/%s --label %s --state open",
            self.owner,
            self.repo,
            self.system,
        )
        self.logger.info(
            "Dry run: gh issue comment %s -R %s/%s --body \"[yt-claim] ...\"",
            issue,
            self.owner,
            self.repo,
        )
        self.logger.info("Dry run: would move status to in progress")
        self.logger.info("Dry run: would create temp dir under %s", self.tmp_root or "<system tmp>")
        self.logger.info("Dry run: git clone git@github.com:%s/%s.git", self.owner, self.repo)
        self.logger.info("Dry run: git checkout -b %s", branch)
        self.logger.info("Dry run: python -m pytest -q")
        self.logger.info("Dry run: python scripts/run_eval.py --mode hf --hf-model <hf_model_id>")
        self.logger.info("Dry run: python scripts/run_eval.py --mode tt --trace 0 --hf-model <hf_model_id>")
        self.logger.info("Dry run: python scripts/run_eval.py --mode tt --trace 1 --hf-model <hf_model_id>")
        self.logger.info("Dry run: tt-smi reset")
        self.logger.info("Dry run: gh pr create -R %s/%s --base main --head %s", self.owner, self.repo, branch)
        self.logger.info("Dry run: gh issue comment %s -R %s/%s --body \"[yt-status] ...\"", issue, self.owner, self.repo)

    def _claim_issue(self, number, run_id):
        claim = _claim_comment(self.worker_name, self.system, run_id)
        self.github.comment_issue(number, claim)
        latest = self.github.get_latest_claim(number)
        if not latest:
            return False
        if latest.get("author") != self.github.viewer_login():
            self.github.delete_last_comment(number)
            return False
        if latest.get("run_id") != run_id:
            self.github.delete_last_comment(number)
            return False
        return True

    def _run_task(self, number, run_id):
        issue = self.github.get_issue(number)
        fields = parse_issue_body(issue.get("body") or "")
        hf_model_id = fields.get("hf_model_id")
        if not hf_model_id:
            raise SetupError("Missing hf_model_id in issue body")
        hf_revision = fields.get("hf_revision") or None
        prefill_len = _int_or_none(fields.get("prefill_len"))
        decode_len = _int_or_none(fields.get("decode_len"))
        batch = _int_or_none(fields.get("batch"))

        task = FunctionalBringupTask(
            issue_number=number,
            run_id=run_id,
            hf_model_id=hf_model_id,
            hf_revision=hf_revision,
            system=self.system,
            top1_min=self.top1_min,
            top5_min=self.top5_min,
            max_attempts=self.max_attempts,
            owner=self.owner,
            repo=self.repo,
            worker_name=self.worker_name,
            tmp_root=self.tmp_root,
            prefill_len=prefill_len,
            decode_len=decode_len,
            batch=batch,
            shell=self.shell,
            github=self.github,
            logger=self.logger,
        )
        return task()

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


def detect_system(shell):
    """Infer system type from tt-smi output."""
    result = shell.run(["tt-smi", "-ls"], check=False)
    if result.returncode != 0:
        raise RuntimeError("Failed to run tt-smi -ls to detect YT_SYSTEM")
    return parse_system_from_ttsmi_output(result.stdout)


def parse_system_from_ttsmi_output(output):
    """Parse tt-smi output and return n150, n300, or lb."""
    text = (output or "").lower()
    if "n150" in text:
        return "n150"
    n300_l = re.findall(r"\bn300\s+l\b", text)
    if len(n300_l) >= 4:
        return "lb"
    if n300_l:
        return "n300"
    raise RuntimeError("Unable to detect system type from tt-smi output")


def main():
    """Worker CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Run the TTNN bringup worker")
    parser.add_argument("--dry-run", action="store_true", help="Print actions only")
    parser.add_argument("--issue", type=int, help="Issue number for dry-run output")
    parser.add_argument("--once", action="store_true", help="Run a single cycle")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    worker = Worker(dry_run=args.dry_run, dry_run_issue=args.issue)
    while True:
        did_work = worker.run_once()
        if args.once:
            return
        if not did_work:
            time.sleep(worker.sleep_secs)


if __name__ == "__main__":
    main()
