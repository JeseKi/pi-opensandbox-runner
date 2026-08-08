.PHONY: lint test check init-config docs-serve docs-build

lint:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest -q

check: lint test

init-config:
	./scripts/init-config.sh

docs-serve:
	uv run --group docs mkdocs serve

docs-build:
	uv run --group docs mkdocs build --strict
