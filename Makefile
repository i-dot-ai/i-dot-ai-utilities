test:
	PACKAGE_DIRS="logging"; \
	for dir in $$PACKAGE_DIRS; do \
	uv run pytest \
		src/i_dot_ai_utilities/$$dir \
		--cov src/i_dot_ai_utilities/$$dir --cov-report term-missing --cov-fail-under 88 || exit 1; \
	done

lint:
	uv run ruff check
	uv run ruff format --check
	uv run mypy src/i_dot_ai_utilities/ --ignore-missing-imports
	uv run bandit -ll -r src/i_dot_ai_utilities
