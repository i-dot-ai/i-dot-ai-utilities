from collections.abc import Generator
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

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
def azure_file_store() -> FileStore:
    return create_file_store(FileStoreDestinationEnum.AZURE_BLOB_STORAGE, define_logger())


@pytest.fixture
def blob_client(azure_file_store: FileStore) -> BlobServiceClient:
    blob_client: BlobServiceClient = cast("BlobServiceClient", azure_file_store.get_client())
    return blob_client


@pytest.fixture
def container(blob_client: BlobServiceClient) -> Generator[Any, Any, None]:
    try:
        container_client = blob_client.get_container_client(settings.bucket_name)
        container_client.get_container_properties()  # Check if container exists
    except ResourceNotFoundError:
        blob_client.create_container(settings.bucket_name)

    yield

    container_client = blob_client.get_container_client(settings.bucket_name)
    blobs = container_client.list_blobs()
    for blob in blobs:
        container_client.delete_blob(blob.name)

    blob_client.delete_container(settings.bucket_name)


@pytest.fixture
def file(azure_file_store: FileStore) -> Generator[Any, Any, None]:
    response = azure_file_store.put_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response
    yield
    azure_file_store.delete_object("test_file.txt")
