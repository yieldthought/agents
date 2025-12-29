#!/usr/bin/env bash
set -euo pipefail

check_cmd() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Missing required command: $name"
    return 1
  fi
}

echo "== Checking required commands =="
check_cmd python || exit 1
check_cmd git || exit 1
check_cmd gh || exit 1
check_cmd codex || echo "Warning: codex CLI not found"
check_cmd tt-smi || echo "Warning: tt-smi not found"

if python -c "import codexapi" >/dev/null 2>&1; then
  echo "codexapi: ok"
else
  echo "codexapi: missing (pip install -r requirements.txt)"
fi

if python -c "import ttnn" >/dev/null 2>&1; then
  echo "ttnn import: ok"
else
  echo "ttnn import: missing"
  exit 1
fi

echo "== Checking gh auth =="
if gh auth status >/dev/null 2>&1; then
  echo "gh auth: ok"
else
  echo "gh auth: not configured"
  exit 1
fi

if gh auth status 2>/dev/null | grep -qi "project"; then
  echo "gh auth project scope: ok"
else
  echo "gh auth project scope: missing"
  echo "Run: gh auth refresh -s project"
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN not set (warning for gated models)"
else
  echo "HF_TOKEN: set"
fi

if command -v tt-smi >/dev/null 2>&1; then
  tt-smi --help >/dev/null 2>&1 || echo "tt-smi did not respond"
fi

echo "Bootstrap complete"
