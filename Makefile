-include .env
export

test:
	export IAI_FS_BUCKET_NAME=test-bucket && \
	export STORAGE_EMULATOR_HOST=http://localhost:9023 && \
	docker compose up -d --wait minio gcs-emulator && \
	PACKAGE_DIRS="logging,metrics,file_store"; \
	IFS=,; for dir in $$PACKAGE_DIRS; do \
	uv run pytest \
		src/i_dot_ai_utilities/$$dir \
		--cov-config=.coveragerc \
		--cov src/i_dot_ai_utilities/$$dir \
		--cov-report term-missing \
		--cov-fail-under 75 || exit 1; \
	done; \
	docker compose down minio gcs-emulator

lint:
	uv run ruff format
	uv run ruff check --fix
	uv run mypy src/i_dot_ai_utilities/ --ignore-missing-imports
	uv run bandit -ll -r src/i_dot_ai_utilities
