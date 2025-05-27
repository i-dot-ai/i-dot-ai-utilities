test:
	uv run pytest \
	--cov automatilib --cov-report term-missing --cov-fail-under 88

lint:
	uv run ruff check
	uv run ruff format --check
	uv run mypy src/i_dot_ai_utilities/ --ignore-missing-imports
	uv run bandit -ll -r src/i_dot_ai_utilities
