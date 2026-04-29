#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source .venv/bin/activate
local-codex-bridge serve --config "${HOME}/.local-codex-bridge/config.toml"
