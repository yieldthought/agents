"""Shell execution helpers with logging and error context."""

import json
import logging
import os
import shlex
import subprocess


def format_command(cmd):
    """Format a command for logs."""
    if isinstance(cmd, str):
        return cmd
    return " ".join(shlex.quote(str(part)) for part in cmd)


def tail_lines(text, count=40):
    """Return the last N lines from text."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-count:])


class Shell:
    """Run shell commands and surface failures with context."""

    def __init__(self, logger=None, env=None):
        self.logger = logger or logging.getLogger(__name__)
        self.env = env or os.environ.copy()

    def run(self, cmd, cwd=None, check=True):
        """Run a command and return the CompletedProcess."""
        display = format_command(cmd)
        self.logger.info("cmd: %s", display)
        result = subprocess.run(
            cmd,
            cwd=os.fspath(cwd) if cwd else None,
            env=self.env,
            text=True,
            capture_output=True,
        )
        if check and result.returncode != 0:
            stdout_tail = tail_lines(result.stdout)
            stderr_tail = tail_lines(result.stderr)
            raise RuntimeError(
                "Command failed: "
                f"{display}\n"
                f"exit={result.returncode}\n"
                f"stdout tail:\n{stdout_tail}\n"
                f"stderr tail:\n{stderr_tail}"
            )
        return result

    def run_json(self, cmd, cwd=None):
        """Run a command and parse JSON from stdout."""
        result = self.run(cmd, cwd=cwd, check=True)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            stdout_tail = tail_lines(result.stdout)
            raise RuntimeError(
                "Failed to parse JSON output.\n"
                f"stdout tail:\n{stdout_tail}"
            ) from exc
