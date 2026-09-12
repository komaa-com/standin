.PHONY: help install hooks check fix test py-check py-test py-fix ts-check ts-test ts-build conformance protocol-generate protocol-check docs-check build release-check

CYAN := \033[36m
GREEN := \033[32m
BOLD := \033[1m
RESET := \033[0m

.DEFAULT_GOAL := help

help: ## Show this help
	@echo "$(BOLD)$(CYAN)StandIn$(RESET)  -  one repo, two languages, one wire protocol"
	@echo ""
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "  Python  libraries/python/      ONE package -> standin-sdk"
	@echo "  TS      libraries/typescript/  ONE package -> @komaa/standin-sdk"
	@echo "  The wire protocol both speak:  protocol/   (shared, so it sits above both)"

install: ## Install both halves
	@cd libraries/python && uv sync --all-extras
	@cd libraries/typescript && pnpm install
	@echo "$(BOLD)$(GREEN)ready$(RESET)"

# ---- python -----------------------------------------------------------------

py-check: ## Format, lint and test the Python half
	@cd libraries/python && uv run ruff format --check . && uv run ruff check . && uv run pytest -q

py-test: ## Test the Python half
	@cd libraries/python && uv run pytest -q

py-fix: ## Autofix the Python half
	@cd libraries/python && uv run ruff format . && uv run ruff check --fix .

# ---- typescript -------------------------------------------------------------

ts-check: ## Build, type-check and test the TypeScript half
	@# build first: the plugins typecheck against the emitted .d.ts, so a
	@# stale dist/ hides a real API break and reports a fake one.
	@cd libraries/typescript && pnpm build && pnpm typecheck && pnpm test

ts-test: ## Test the TypeScript half
	@cd libraries/typescript && pnpm test

ts-build: ## Build the TypeScript package
	@cd libraries/typescript && pnpm build

# ---- both -------------------------------------------------------------------

protocol-generate: ## Regenerate both SDK bindings from the pinned call schema
	@python3 protocol/generate.py

protocol-check: ## Fail if either generated binding drifts from the call schema
	@bash protocol/check-drift.sh
	@python3 -m unittest discover -s protocol -p 'test_*.py' -q

docs-check: ## Fail on a broken link, a stale page or a boundary the docs must not cross
	@python3 docs/check.py

hooks: ## Point git at .githooks, so a push runs what CI runs
	@git config core.hooksPath .githooks
	@chmod +x .githooks/*
	@echo "$(BOLD)$(GREEN)pre-push installed$(RESET)  git push now runs make check first; --no-verify skips it"

build: ## Build both packages into their dist directories
	@cd libraries/python && uv build
	@cd libraries/typescript && pnpm build

release-check: ## Build both, and refuse a release whose versions disagree
	@python3 scripts/release_check.py

conformance: ## Run ONLY the shared protocol vectors, in both languages
	@echo "$(BOLD)$(CYAN)python$(RESET)"
	@cd libraries/python && uv run pytest tests/test_conformance.py tests/test_hmac_conformance.py -q
	@echo "$(BOLD)$(CYAN)typescript$(RESET)"
	@cd libraries/typescript && pnpm exec vitest run conformance --reporter=basic

test: py-test ts-test ## Test both halves

check: protocol-check py-check ts-check docs-check ## Everything CI runs, both halves
	@echo "$(BOLD)$(GREEN)all checks passed, both languages$(RESET)"

fix: py-fix ## Autofix what can be autofixed
