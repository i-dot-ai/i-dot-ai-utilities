test:
	uv run pytest \
	--cov automatilib --cov-report term-missing --cov-fail-under 88

lint:
	poetry run ruff check
	poetry run ruff format --check
	poetry run mypy src/i-dot-ai-utilities/  --ignore-missing-imports
	poetry run bandit -ll -r src/i-dot-ai-utilities
