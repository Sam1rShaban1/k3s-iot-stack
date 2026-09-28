# Benchmark harness and validation tasks.
#
# Replaces the ad-hoc build/run steps previously embedded in run_test.sh.

PUBLISHER := publisher
SRC       := publisher.c
PTHREAD   := -lpaho-mqtt3c
CFLAGS    := -Wall -Wextra -O2

.DEFAULT_GOAL := help
.PHONY: help build test test-fast lint validate preflight config run clean-publishers

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

build: $(PUBLISHER) ## Compile the MQTT load generator

$(PUBLISHER): $(SRC)
	gcc $(CFLAGS) $(SRC) -o $@ $(PTHREAD)
	@echo "built $@"

test: build ## Run the full test suite
	python3 -m unittest discover -s harness/tests -p "test_*.py" -v

test-fast: ## Run only the tests that need no broker and no compiler
	python3 -m unittest harness.tests.test_regressions harness.tests.test_detectors -v

lint: ## Syntax-check the C source, the Python modules, and the YAML tree
	@python3 -m compileall -q harness >/dev/null && echo "python: ok"
	@gcc $(CFLAGS) -fsyntax-only $(SRC) && echo "c: ok"
	@python3 -m harness validate >/dev/null && echo "yaml: ok"

validate: lint test ## Lint then test

preflight: ## Verify the cluster and pipeline before benchmarking
	python3 -m harness preflight

config: ## Print the resolved harness configuration
	python3 -m harness config

run: build preflight ## Preflight, then run the full scenario matrix
	python3 -m harness run

clean-publishers: ## Kill any stray publishers (legacy escape hatch)
	-pkill -f '$(PUBLISHER)' 2>/dev/null || true
