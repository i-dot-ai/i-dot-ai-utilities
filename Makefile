-include .env
export

run_backing_services:
	docker compose up -d --wait

test:
	export IAI_FS_BUCKET_NAME=test-bucket && \
	export IAI_LITELLM_API_BASE=http://localhost:4000 && \
	export IAI_LITELLM_API_KEY=sk-1234567890abcdef && \
	export LITELLM_MASTER_KEY=sk-1234567890abcdef && \
	export IAI_LITELLM_CHAT_MODEL=azure/o4-mini && \
	export IAI_LITELLM_EMBEDDING_MODEL=text-embedding-3-small && \
	export IAI_LITELLM_PROJECT_NAME=utilities-tests && \
	docker compose up -d --wait && \
	PACKAGE_DIRS="logging,metrics,file_store,litellm,auth"; \
	IFS=,; for dir in $$PACKAGE_DIRS; do \
	uv run pytest \
		src/i_dot_ai_utilities/$$dir \
		--cov-config=.coveragerc \
		--cov src/i_dot_ai_utilities/$$dir \
		--cov-report term-missing \
		--cov-fail-under 75 || exit 1; \
	done; \
	docker compose down

lint:
	uv run ruff format --check
	uv run ruff check
	uv run mypy src/i_dot_ai_utilities/ --ignore-missing-imports
	uv run bandit -ll -r src/i_dot_ai_utilities
