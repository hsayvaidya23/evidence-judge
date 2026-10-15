UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test e2e fixtures schema svgs media bench ci

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

test: ## Unit tests with coverage, then the wall-clock budgets without it
	$(UV) run pytest tests/unit -m "not live and not perf" --cov=evidence_judge --cov-report=term-missing
	$(UV) run pytest tests/unit -m perf

e2e: ## End-to-end tests against a real browser
	$(UV) run pytest -m e2e

fixtures: ## Re-record the demo fixtures (traces, HAR, screenshots)
	$(UV) run python scripts/record_fixtures.py

schema: ## Regenerate schemas/run-verdict.schema.json
	$(UV) run evidence-judge schema --out schemas/run-verdict.schema.json

svgs: ## Regenerate the README SVGs from the fixtures
	$(UV) run python scripts/make_readme_svgs.py

media: ## Re-render the README animations and report screenshots (~9 min)
	$(UV) run --with pillow python -m scripts.make_readme_media

bench: ## Re-run the offline benchmark into bench/results/
	$(UV) run evidence-judge bench tests/fixtures/runs --seeds 3 \
		--out bench/results/offline-fixtures.json --markdown bench/results/offline-fixtures.md

ci: lint typecheck test ## Everything CI runs except e2e
