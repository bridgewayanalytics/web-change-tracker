# web-change-tracker - local and CI commands

PYTHON := python3
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: install install-playwright run lint test ci clean

# Create venv and install dependencies
install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements.txt

# Install Playwright Chromium (optional; falls back to requests if missing)
install-playwright:
	$(VENV)/bin/playwright install chromium

# Run the change-detection pipeline locally
run:
	$(PY) spike.py

# Run with requests-only (no Playwright); useful if Chromium unavailable
run-requests:
	USE_PLAYWRIGHT=0 $(PY) spike.py

# Lint Python files
lint:
	$(PIP) install ruff -q 2>/dev/null || true
	$(VENV)/bin/ruff check spike.py state_store.py 2>/dev/null || $(PY) -m py_compile spike.py state_store.py

# Run tests (placeholder; add pytest when tests exist)
test:
	@echo "No tests yet. Add tests/ and run: $(PY) -m pytest"
	@test -d tests && $(PY) -m pytest tests/ -v || true

# CI: install, lint, run once (uses requests only, no Playwright)
ci: install lint
	USE_PLAYWRIGHT=0 $(PY) spike.py

# Remove venv and generated files
clean:
	rm -rf $(VENV) __pycache__ .ruff_cache
	rm -f state.json last_report.txt
