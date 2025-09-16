-include .env
export

test:
	export IAI_FS_BUCKET_NAME=test-bucket && \
	export STORAGE_EMULATOR_HOST=http://localhost:9023 && \
	export IAI_FS_AZURE_ACCOUNT_URL=http://localhost:10000/devstoreaccount1 && \
	export IAI_FS_AZURE_CONNECTION_STRING="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://localhost:10000/devstoreaccount1;" && \  # pragma: allowlist secret
	docker compose up -d --wait minio gcs-emulator azurite && \
	PACKAGE_DIRS="logging,metrics,file_store"; \
	IFS=,; for dir in $$PACKAGE_DIRS; do \
	uv run pytest \
		src/i_dot_ai_utilities/$$dir \
		--cov-config=.coveragerc \
		--cov src/i_dot_ai_utilities/$$dir \
		--cov-report term-missing \
		--cov-fail-under 75 || exit 1; \
	done; \
	docker compose down minio gcs-emulator azurite

lint:
	uv run ruff format
	uv run ruff check --fix
	uv run mypy src/i_dot_ai_utilities/ --ignore-missing-imports
	uv run bandit -ll -r src/i_dot_ai_utilities
