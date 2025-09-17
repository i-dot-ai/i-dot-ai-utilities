from functools import lru_cache

from i_dot_ai_utilities.file_store.aws_s3.main import S3FileStore
from i_dot_ai_utilities.file_store.azure_blob_storage.main import AzureFileStore
from i_dot_ai_utilities.file_store.gcp_cloud_storage.main import GCPFileStore
from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.file_store.types.file_store_destination_enum import FileStoreDestinationEnum
from i_dot_ai_utilities.file_store.types.kwargs_dicts import AzureClientKwargs, GCPClientKwargs, S3ClientKwargs
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def create_file_store(
    destination: FileStoreDestinationEnum,
    logger: StructuredLogger,
    **kwargs: S3ClientKwargs | AzureClientKwargs | GCPClientKwargs,
) -> FileStore:
    stores = {
        FileStoreDestinationEnum.AWS_S3: S3FileStore,
        FileStoreDestinationEnum.GCP_CLOUD_STORAGE: GCPFileStore,
        FileStoreDestinationEnum.AZURE_BLOB_STORAGE: AzureFileStore,
    }

    if destination not in stores:
        raise ValueError("Unsupported destination: " + destination.name)

    settings = get_settings()
    return stores[destination](logger, settings, **kwargs)  # type: ignore[no-any-return]
