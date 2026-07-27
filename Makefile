.PHONY: lint test check

lint:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest -q

check: lint test
