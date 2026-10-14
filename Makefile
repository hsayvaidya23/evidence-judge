UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test e2e fixtures schema ci

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dependencies and the Playwright browser
	$(UV) sync --locked
	$(UV) run playwright install chromium

lint: ## Lint and check formatting
	$(UV) run ruff check
	$(UV) run ruff format --check

fmt: ## Auto-fix lint issues and format
	$(UV) run ruff check --fix
	$(UV) run ruff format

typecheck: ## Strict type check
	$(UV) run pyright

test: ## Unit tests with coverage
	$(UV) run pytest tests/unit --cov=evidence_judge --cov-report=term-missing

e2e: ## End-to-end tests against a real browser
	$(UV) run pytest -m e2e

fixtures: ## Re-record the demo fixtures (traces, HAR, screenshots)
	$(UV) run python scripts/record_fixtures.py

schema: ## Regenerate schemas/run-verdict.schema.json
	$(UV) run evidence-judge schema --out schemas/run-verdict.schema.json

ci: lint typecheck test ## Everything CI runs except e2e
