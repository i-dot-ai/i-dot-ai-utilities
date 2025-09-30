from abc import ABC, abstractmethod
from typing import Any, BinaryIO

from azure.storage.blob import BlobServiceClient
from google.cloud.storage import Client
from mypy_boto3_s3.client import S3Client


class FileStore(ABC):
    @abstractmethod
    def get_client(self) -> S3Client | BlobServiceClient | Client:
        pass

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

    @abstractmethod
    def list_buckets(
        self,
    ) -> list[dict]:
        pass

    @abstractmethod
    def create_bucket(self, name: str) -> None:
        pass
