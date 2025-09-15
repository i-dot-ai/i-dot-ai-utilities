from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any, BinaryIO

from i_dot_ai_utilities.file_store.aws_s3.main import S3FileStore
from i_dot_ai_utilities.file_store.azure_blob_storage.main import AzureFileStore
from i_dot_ai_utilities.file_store.enums.file_store_destination import FileStoreDestinationEnum
from i_dot_ai_utilities.file_store.gcp_cloud_storage.main import GCPFileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.file_store.types.kwargs_dicts import AzureClientKwargs, GCPClientKwargs, S3ClientKwargs
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


@lru_cache
def get_settings() -> Settings:
    return Settings()


class FileStore(ABC):
    @classmethod
    def create(
        cls,
        destination: FileStoreDestinationEnum,
        logger: StructuredLogger,
        **kwargs: S3ClientKwargs | AzureClientKwargs | GCPClientKwargs,
    ) -> "FileStore":
        stores = {
            FileStoreDestinationEnum.AWS_S3: S3FileStore,
            FileStoreDestinationEnum.GCP_CLOUD_STORAGE: GCPFileStore,
            FileStoreDestinationEnum.AZURE_BLOB_STORAGE: AzureFileStore,
        }

        if destination not in stores:
            raise ValueError("Unsupported destination: " + destination.value)
        settings = get_settings()
        filestore: S3FileStore | GCPFileStore | AzureFileStore = stores[destination](logger, settings, **kwargs)
        return filestore

    @abstractmethod
    def read_object(self, key: str, as_text: bool = False, encoding: str = "utf-8") -> bytes | str | None:
        pass

    @abstractmethod
    def put_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        pass

    @abstractmethod
    def update_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        pass

    @abstractmethod
    def delete_object(self, key: str) -> bool:
        pass

    @abstractmethod
    def object_exists(self, key: str) -> bool:
        pass

    @abstractmethod
    def download_object_url(self, key: str, expiration: int = 3600) -> str | None:
        pass

    @abstractmethod
    def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict[str, str | int]]:
        pass

    @abstractmethod
    def get_object_metadata(
        self,
        key: str,
    ) -> dict[str, str | int | dict[str, Any]] | None:
        pass

    @abstractmethod
    def copy_object(
        self,
        source_key: str,
        dest_key: str,
    ) -> bool:
        pass

    @abstractmethod
    def upload_json(
        self,
        key: str,
        data: dict | list,
        metadata: dict[str, str] | None = None,
    ) -> bool:
        pass

    @abstractmethod
    def download_json(
        self,
        key: str,
    ) -> dict | list | None:
        pass
