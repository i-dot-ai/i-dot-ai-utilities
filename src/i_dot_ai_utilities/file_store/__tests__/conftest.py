import os
from collections.abc import Generator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat


def set_environment_variables() -> None:
    os.environ["ENVIRONMENT"] = "local"
    os.environ["IAI_FS_BUCKET_NAME"] = "test-bucket"
    os.environ["IAI_FS_AWS_REGION"] = "eu-west-2"
    os.environ["IAI_FS_MINIO_ADDRESS"] = "http://localhost:9000"
    os.environ["IAI_FS_AWS_ACCESS_KEY_ID"] = "minioadmin"
    os.environ["IAI_FS_AWS_SECRET_ACCESS_KEY"] = "minioadmin"  # noqa: S105
    os.environ["IAI_FS_DATA_DIR"] = "app_data"


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
def settings() -> Settings:
    set_environment_variables()
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def file_store() -> FileStore:
    return FileStore(define_logger())


@pytest.fixture
def client(settings: Settings) -> boto3.client:
    return settings.boto3_client()


@pytest.fixture
def bucket(client: boto3.client, settings: Settings) -> Generator[Any, Any, None]:
    try:
        client.head_bucket(Bucket=settings.bucket_name)
    except ClientError:
        client.create_bucket(Bucket=settings.bucket_name)
    yield
    objects = client.list_objects_v2(Bucket=settings.bucket_name).get("Contents", [])
    if objects:
        objects = [{"Key": x["Key"]} for x in objects]
        client.delete_objects(Bucket=settings.bucket_name, Delete={"Objects": objects})
    client.delete_bucket(Bucket=settings.bucket_name)


@pytest.fixture
def file(file_store: FileStore) -> Generator[Any, Any, None]:
    response = file_store.create_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response
    yield
    response = file_store.delete_object("test_file.txt")
    assert response
