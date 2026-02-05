#!/usr/bin/env bash
# Run web-change-tracker locally.
# Usage: ./scripts/run-local.sh
# Or: make run (which uses the venv)

set -e
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "Creating venv and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

.venv/bin/python spike.py
