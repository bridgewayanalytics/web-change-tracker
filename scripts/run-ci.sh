#!/usr/bin/env bash
# CI pipeline: install, lint, run once.
# Usage: ./scripts/run-ci.sh
# Or: make ci

set -e
cd "$(dirname "$0")/.."

echo "=== Installing dependencies ==="
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

echo "=== Linting ==="
.venv/bin/pip install ruff -q 2>/dev/null || true
if .venv/bin/ruff check spike.py state_store.py 2>/dev/null; then
  echo "ruff: OK"
else
  python3 -m py_compile spike.py state_store.py && echo "py_compile: OK"
fi

echo "=== Running change-detection pipeline ==="
USE_PLAYWRIGHT=0 .venv/bin/python spike.py

echo "=== CI complete ==="
