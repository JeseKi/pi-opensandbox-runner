.PHONY: lint test check init-config manager-run docs-serve docs-build

lint:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest -q

check: lint test

init-config:
	./scripts/init-config.sh

manager-run:
	@set -a; . ./.manager.env; set +a; \
	OPENSANDBOX_BASE_URL="$${OPENSANDBOX_BASE_URL:-http://127.0.0.1:8080}" \
	LITELLM_BASE_URL="$${LITELLM_BASE_URL:-http://127.0.0.1:4000}" \
	RUNNER_MANAGER_DOCS_SITE_DIR="$${RUNNER_MANAGER_DOCS_SITE_DIR:-site}" \
	uv run pi-runner-manager

docs-serve:
	uv run --group docs mkdocs serve

docs-build:
	uv run --group docs mkdocs build --strict
