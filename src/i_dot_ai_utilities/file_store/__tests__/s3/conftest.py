from collections.abc import Generator
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError
from mypy_boto3_s3 import S3Client

if TYPE_CHECKING:
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

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
def s3_file_store() -> FileStore:
    return create_file_store(FileStoreDestinationEnum.AWS_S3, define_logger())


@pytest.fixture
def boto3_client(s3_file_store: FileStore) -> S3Client:
    s3_client: S3Client = cast("S3Client", s3_file_store.get_client())
    return s3_client


@pytest.fixture
def bucket(boto3_client: S3Client) -> Generator[Any, Any, None]:
    try:
        boto3_client.head_bucket(Bucket=settings.bucket_name)
    except ClientError:
        boto3_client.create_bucket(Bucket=settings.bucket_name)
    yield
    objects = boto3_client.list_objects_v2(Bucket=settings.bucket_name).get("Contents", [])
    if objects:
        delete_objects: list[ObjectIdentifierTypeDef] = [{"Key": obj["Key"]} for obj in objects]
        boto3_client.delete_objects(Bucket=settings.bucket_name, Delete={"Objects": delete_objects})
    boto3_client.delete_bucket(Bucket=settings.bucket_name)


@pytest.fixture
def file(s3_file_store: FileStore) -> Generator[Any, Any, None]:
    response = s3_file_store.put_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response
    yield
    response = s3_file_store.delete_object("test_file.txt")
    assert response
