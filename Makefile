# Telegram Bulk Exporter — dev Makefile
# On Windows: use `make` from Git Bash / WSL. Most targets are one-shot commands.

.PHONY: help sync run test test-crypto test-tdl lint fmt type clean reset-dev docker-build docker-run

help:
	@echo "make sync         - uv sync incl. dev deps"
	@echo "make run          - run the server on localhost:8765"
	@echo "make test         - run full pytest suite"
	@echo "make lint         - ruff check"
	@echo "make fmt          - ruff check --fix + ruff format"
	@echo "make type         - mypy non-strict"
	@echo "make docker-build - build the container image"
	@echo "make docker-run   - docker-compose up"
	@echo "make reset-dev    - wipe ./data and ./exports (irrecoverable)"

sync:
	uv sync --extra dev

run:
	uv run python -m src.main

test:
	uv run pytest -q

test-crypto:
	uv run pytest -q -k "crypto or auth or vault"

test-tdl:
	uv run pytest -q -k "tdl"

lint:
	uv run ruff check src/ tests/

fmt:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

type:
	uv run mypy src/

reset-dev:
	rm -rf data exports
	mkdir -p data exports
	touch data/.keep exports/.keep

docker-build:
	docker build -t telegram-bulk-exporter:dev .

docker-run:
	docker compose up -d --build

clean:
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info
