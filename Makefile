.PHONY: install run scan run-queue doctor test lint

install:
	uv pip install -e ".[dev]"

run:
	.venv/bin/tribe-agent run

scan:
	.venv/bin/tribe-agent scan $(NAME)

run-queue:
	.venv/bin/tribe-agent run-queue

doctor:
	.venv/bin/tribe-agent doctor

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check src tests
