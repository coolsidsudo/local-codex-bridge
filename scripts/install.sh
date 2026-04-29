#!/usr/bin/env bash
set -euo pipefail

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

mkdir -p "${HOME}/.local-codex-bridge"
if [ ! -f "${HOME}/.local-codex-bridge/config.toml" ]; then
  cp config.example.toml "${HOME}/.local-codex-bridge/config.toml"
  echo "Created ${HOME}/.local-codex-bridge/config.toml"
fi

echo "Installed local-codex-bridge."
echo "Edit ~/.local-codex-bridge/config.toml, then run:"
echo "  source .venv/bin/activate"
echo "  local-codex-bridge serve --config ~/.local-codex-bridge/config.toml"
