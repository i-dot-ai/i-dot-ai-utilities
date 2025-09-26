from collections.abc import Generator
from typing import Any, cast

import pytest
from google.cloud import storage
from google.cloud.exceptions import NotFound

from i_dot_ai_utilities.file_store.factory import create_file_store
from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.file_store.types.file_store_destination_enum import FileStoreDestinationEnum
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat

settings = Settings()  # type: ignore[call-arg]


def define_logger() -> StructuredLogger:
    logger_environment = ExecutionEnvironmentType.LOCAL
    logger_format = LogOutputFormat.TEXT

    return StructuredLogger(
        level="info",
        options={
            "execution_environment": logger_environment,
            "log_format": logger_format,
        },
    )


@pytest.fixture
def gcp_file_store() -> FileStore:
    return create_file_store(FileStoreDestinationEnum.GCP_CLOUD_STORAGE, define_logger())


@pytest.fixture
def gcs_client(gcp_file_store: FileStore) -> storage.Client:
    gcs_client: storage.Client = cast("storage.Client", gcp_file_store.get_client())
    return gcs_client


@pytest.fixture
def bucket(gcs_client: storage.Client) -> Generator[Any, Any, None]:
    try:
        bucket = gcs_client.bucket(settings.bucket_name)
        bucket.reload()  # Check if bucket exists
    except NotFound:
        bucket = gcs_client.create_bucket(settings.bucket_name)

    yield

    # Clean up all objects in the bucket
    blobs = list(bucket.list_blobs())
    if blobs:
        bucket.delete_blobs(blobs)

    # Delete the bucket
    bucket.delete()


@pytest.fixture
def file(gcp_file_store: FileStore) -> Generator[Any, Any, None]:
    response = gcp_file_store.put_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response
    yield
    gcp_file_store.delete_object("test_file.txt")
