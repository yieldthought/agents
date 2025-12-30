#!/usr/bin/env bash
set -euo pipefail

echo "== Installing uv =="
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  elif [ -x "$HOME/.cargo/bin/uv" ]; then
    export PATH="$HOME/.cargo/bin:$PATH"
  fi
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found after install"
  exit 1
fi

echo "== Installing gh CLI =="
if ! command -v gh >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y gh
  elif command -v brew >/dev/null 2>&1; then
    brew install gh
  else
    echo "gh not found and no supported package manager detected."
    echo "Install manually from https://github.com/cli/cli#installation"
    exit 1
  fi
fi

echo "== Installing Node + Codex CLI =="
if ! command -v npm >/dev/null 2>&1; then
  if [ ! -d "$HOME/.nvm" ]; then
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
  fi
  export NVM_DIR="$HOME/.nvm"
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1090
    . "$NVM_DIR/nvm.sh"
  fi
  nvm install --lts
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found after install"
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "node not found after install"
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  npm install -g @openai/codex
fi
if ! command -v codex >/dev/null 2>&1; then
  export PATH="$(npm bin -g):$PATH"
fi
if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not found after npm install"
  exit 1
fi

echo "== Creating uv environment =="
VENV_DIR="${YT_VENV_DIR:-$HOME/.venvs/yt-agents}"
if [ ! -d "$VENV_DIR" ]; then
  uv venv "$VENV_DIR"
fi
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

PIP_INDEX_ARGS=()
if [ -n "${YT_PIP_INDEX_URL:-}" ]; then
  PIP_INDEX_ARGS+=(--index-url "$YT_PIP_INDEX_URL")
fi
if [ -n "${YT_PIP_EXTRA_INDEX_URL:-}" ]; then
  PIP_INDEX_ARGS+=(--extra-index-url "$YT_PIP_EXTRA_INDEX_URL")
fi

uv pip install "${PIP_INDEX_ARGS[@]}" codexapi huggingface_hub

if python -c "import ttnn" >/dev/null 2>&1; then
  echo "ttnn import: ok"
else
  TTNN_SPEC="${YT_TTNN_SPEC:-ttnn}"
  echo "Installing ttnn from: $TTNN_SPEC"
  if ! uv pip install "${PIP_INDEX_ARGS[@]}" "$TTNN_SPEC"; then
    echo "Failed to install ttnn."
    echo "Set YT_TTNN_SPEC to a wheel path or custom spec, or set YT_PIP_INDEX_URL."
    exit 1
  fi
fi

if command -v tt-smi >/dev/null 2>&1; then
  echo "tt-smi: ok"
else
  TT_SMI_SPEC="${YT_TT_SMI_SPEC:-tt-smi}"
  echo "Installing tt-smi from: $TT_SMI_SPEC"
  if ! uv pip install "${PIP_INDEX_ARGS[@]}" "$TT_SMI_SPEC"; then
    echo "Failed to install tt-smi."
    echo "Set YT_TT_SMI_SPEC or install tt-smi separately."
  fi
fi

echo "== Installing agents repo =="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
uv pip install -e "$AGENTS_DIR"

echo "== Checking required commands =="
command -v python >/dev/null 2>&1 || { echo "Missing required command: python"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "Missing required command: git"; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "Missing required command: gh"; exit 1; }
command -v tt-smi >/dev/null 2>&1 || echo "Warning: tt-smi not found in PATH"

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

if command -v hf >/dev/null 2>&1; then
  if hf auth whoami >/dev/null 2>&1; then
    echo "hf auth: ok"
  else
    echo "hf auth: not configured (run: hf auth login)"
  fi
else
  echo "hf CLI not found"
fi

echo "Bootstrap complete"
echo "Activate with: source \"$VENV_DIR/bin/activate\""
