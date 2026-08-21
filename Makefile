# ai-batch-runner engineering Makefile (modeled on snaplink's Makefile).
#
# Zero-dependency gates (filesize/complexity/root-policy/style + harness
# self-tests) always work; `test` and `coverage` need the dev extras:
#   pip install -e ".[dev]"
#
# make ci = the full gate set (what CI runs).

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# -- gates (zero dependencies) ----------------------------------------
check: ## Run the registered project engineering gates (fail closed).
	python pi-batch.py check --skip-eval

check-filesize: ## File size budget (500 lines by default).
	python cli.py check-filesize

check-complexity: ## Cyclomatic (15) / cognitive (20) budgets per function.
	python cli.py check-complexity

check-root: ## Root directory policy (thin launcher layer only).
	python cli.py check-root

check-style: ## Whitespace hygiene (trailing space, tabs, final newline).
	python cli.py check-style

self-test: ## Harness integrity: the gates' own unit tests (zero-dep unittest).
	python cli.py check-test

quality: ## Upstream-synced org scanner (quality.py): func lines, complexity, dup bodies.
	python quality.py .

harness: check self-test ## pre-push gate (snaplink naming).

# -- test / coverage (need dev extras) --------------------------------
test: ## Regression suite (tests/). Requires: pip install -e ".[dev]".
	python -m pytest tests/

eval: ## Rule-system regression suite (evals/, pi-batch eval).
	python -m pbatch eval

coverage: ## Coverage report for tests/ + checks/. Requires pytest-cov.
	python -m pytest --cov=checks --cov=cli --cov-report=term-missing tests/ checks/

# -- aggregate ---------------------------------------------------------
ci: check self-test test eval quality org ## Full CI: gates + tests + eval + org scanner + engineering-org specs.

org: ## Engineering-org spec integrity (17 node specs + capability registry + graph).
	python pi-batch.py org check
	python pi-batch.py capabilities check
	python pi-batch.py graph extract --input pbatch

.PHONY: help check check-filesize check-complexity check-root check-style \
        self-test quality harness test eval coverage org ci
