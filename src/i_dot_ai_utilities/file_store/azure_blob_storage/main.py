import json
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Unpack

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.file_store.types.kwargs_dicts import AzureClientKwargs
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


class AzureFileStore(FileStore):
    """
    File storage class providing CRUD operations for Azure Blob Storage objects
    """

    def __init_azure_client(self, **kwargs: Unpack[AzureClientKwargs]) -> BlobServiceClient:
        """
        This function returns the client connection to Azure Blob Storage
        :return: Azure Blob Storage client
        """
        if self.settings.environment.lower() in ["local", "test"]:
            return BlobServiceClient(
                account_url=self.settings.azure_account_url, credential=self.settings.azure_connection_string, **kwargs
            )
        else:
            return BlobServiceClient(
                account_url=self.settings.azure_account_url, credential=self.settings.azure_account_key, **kwargs
            )

    def __init__(self, logger: StructuredLogger, settings: Settings, **kwargs: Unpack[AzureClientKwargs]) -> None:
        """
        Initialize FileStore with Azure Blob Storage client from settings
        :param logger: A `StructuredLogger` instance
        """
        self.logger = logger
        self.settings = settings
        self.client: BlobServiceClient = self.__init_azure_client(**kwargs)
        self.container_client = self.client.get_container_client(self.settings.bucket_name)

    def __prefix_key(self, key: str) -> str:
        """
        Returns the key with a prefix if it's set
        :param key: The Blob Storage object key
        :return: The key with a prefix if it's set
        """
        return key if not self.settings.data_dir else f"{self.settings.data_dir}/{key}"

    def get_client(self) -> BlobServiceClient:
        return self.client

    def put_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        """
        Create/upload an object to Blob Storage.

        Args:
            key: Blob Storage object key (path)
            data: Data to upload (string, bytes, or file-like object)
            metadata: Optional metadata dictionary
            content_type: Optional content type

        Returns:
            bool: True if successful, False otherwise
        """
        container_name = self.settings.bucket_name
        key = self.__prefix_key(key)
        try:
            blob_client = self.container_client.get_blob_client(key)

            upload_kwargs: dict[str, Any] = {}
            if metadata:
                upload_kwargs["metadata"] = metadata
            if content_type:
                upload_kwargs["content_type"] = content_type

            if isinstance(data, str) or hasattr(data, "read"):
                blob_client.upload_blob(data, overwrite=True, **upload_kwargs)
            else:
                blob_client.upload_blob(data, overwrite=True, **upload_kwargs)

            self.logger.info(
                "Successfully uploaded object: {key} to container: {container}", key=key, container=container_name
            )
        except AzureError:
            self.logger.exception("Failed to upload object {key}", key=key)
            return False
        else:
            return True

    def read_object(self, key: str, as_text: bool = False, encoding: str = "utf-8") -> bytes | str | None:
        """
        Read/download an object from Blob Storage.

        Args:
            key: Blob Storage object key (path)
            as_text: If True, return as string, otherwise as bytes
            encoding: Text encoding if as_text is True

        Returns:
            Object content as bytes or string, None if not found
        """
        key = self.__prefix_key(key)
        try:
            blob_client = self.container_client.get_blob_client(key)
            if as_text:
                content: str = blob_client.download_blob().readall().decode(encoding)
                return content
            else:
                content_bytes: bytes = blob_client.download_blob().readall()
                return content_bytes
        except ResourceNotFoundError:
            self.logger.warning("Object not found: {key}", key=key)
            return None
        except AzureError:
            self.logger.exception("Failed to read object {key}", key=key)
            return None

    def update_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        """
        Update an existing object in Blob Storage (same as create_object)

        Args:
            key: Blob Storage object key (path)
            data: New data to upload
            metadata: Optional metadata dictionary
            content_type: Optional content type

        Returns:
            bool: True if successful, False otherwise
        """
        return self.put_object(key, data, metadata, content_type)

    def delete_object(self, key: str) -> bool:
        """
        Delete an object from Blob Storage.

        Args:
            key: Blob Storage object key (path)

        Returns:
            bool: True if successful, False otherwise
        """
        container_name = self.settings.bucket_name
        key = self.__prefix_key(key)
        try:
            blob_client = self.container_client.get_blob_client(key)
            blob_client.delete_blob()
            self.logger.info(
                "Successfully deleted object: {key} from container: {container}", key=key, container=container_name
            )
        except ResourceNotFoundError:
            self.logger.warning("Object not found for deletion: {key}", key=key)
            return False
        except AzureError:
            self.logger.exception("Failed to delete object {key}", key=key)
            return False
        else:
            return True

    def object_exists(self, key: str) -> bool:
        """
        Check if an object exists in Blob Storage

        Args:
            key: Blob Storage object key (path)

        Returns:
            bool: True if object exists, False otherwise
        """
        key = self.__prefix_key(key)
        try:
            blob_client = self.container_client.get_blob_client(key)
            client_exists: bool = blob_client.exists()
        except AzureError:
            self.logger.exception("Error checking object {key} existence", key=key)
            return False
        else:
            return client_exists

    def download_object_url(self, key: str, expiration: int = 3600) -> str | None:
        """
        Get an objects pre-signed URL

        Args:
            key: Blob Storage object key (path)
            expiration: Expiration time in seconds
        Returns:
            str: Blob Storage object pre-signed URL as string. If error, returns None
        """
        try:
            if not self.object_exists(key):
                return None

            blob_client = self.container_client.get_blob_client(self.__prefix_key(key))

            sas_token = generate_blob_sas(
                account_name=blob_client.account_name,
                container_name=blob_client.container_name,
                blob_name=blob_client.blob_name,
                account_key=self.settings.azure_account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(seconds=expiration),
            )
        except AzureError:
            self.logger.exception("Error generating signed URL for {key}", key=self.__prefix_key(key))
            return None
        else:
            return f"{blob_client.url}?{sas_token}"

    def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict[str, str | int]]:
        """
        List objects in Blob Storage container with optional prefix filter

        Args:
            prefix: Optional prefix to filter objects
            max_keys: Maximum number of objects to return

        Returns:
            List of dictionaries containing object information
        """
        prefix = self.__prefix_key(prefix)
        objects = []
        try:
            blob_list = self.container_client.list_blobs(name_starts_with=prefix, results_per_page=max_keys)
            for blob in blob_list:
                objects.append(
                    {
                        "key": blob.name,
                        "size": blob.size or 0,
                        "last_modified": blob.last_modified.isoformat() if blob.last_modified else "",
                        "etag": blob.etag.strip('"') if blob.etag else "",
                    }
                )
        except AzureError:
            self.logger.exception("Failed to list objects with prefix {prefix}", prefix=prefix)
            return []
        else:
            return objects

    def get_object_metadata(
        self,
        key: str,
    ) -> dict[str, str | int | dict[str, Any]] | None:
        """
        Get metadata for a Blob Storage object

        Args:
            key: Blob Storage object key (path)

        Returns:
            Dictionary containing object metadata or None if not found
        """
        key = self.__prefix_key(key)
        try:
            blob_client = self.container_client.get_blob_client(key)
            properties = blob_client.get_blob_properties()

            return {
                "content_length": properties.size or 0,
                "content_type": properties.content_settings.content_type or "",
                "last_modified": properties.last_modified.isoformat() if properties.last_modified else "",
                "etag": properties.etag.strip('"') if properties.etag else "",
                "metadata": properties.metadata or {},
            }

        except ResourceNotFoundError:
            self.logger.warning("Object not found: {key}", key=key)
            return None
        except AzureError:
            self.logger.exception("Failed to get metadata for {key}", key=key)
            return None

    def copy_object(
        self,
        source_key: str,
        dest_key: str,
    ) -> bool:
        """
        Copy an object within Blob Storage

        Args:
            source_key: Source Blob Storage object key
            dest_key: Destination Blob Storage object key

        Returns:
            bool: True if successful, False otherwise
        """
        source_key = self.__prefix_key(source_key)
        dest_key = self.__prefix_key(dest_key)
        try:
            source_blob_client = self.container_client.get_blob_client(source_key)
            dest_blob_client = self.container_client.get_blob_client(dest_key)

            dest_blob_client.start_copy_from_url(source_blob_client.url)
        except AzureError:
            self.logger.exception(
                "Failed to copy object {source_key} to {dest_key}",
                source_key=source_key,
                dest_key=dest_key,
            )
            return False
        else:
            self.logger.info("Successfully copied {source_key} to {dest_key}", source_key=source_key, dest_key=dest_key)
            return True

    def upload_json(
        self,
        key: str,
        data: dict | list,
        metadata: dict[str, str] | None = None,
    ) -> bool:
        """
        Upload JSON data to Blob Storage

        Args:
            key: Blob Storage object key (path)
            data: Dictionary or list to serialize as JSON
            metadata: Optional metadata dictionary

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            json_data = json.dumps(data, indent=2)
            return self.put_object(
                key=key,
                data=json_data,
                metadata=metadata,
                content_type="application/json",
            )
        except (TypeError, ValueError):
            self.logger.exception("Failed to serialize data as JSON")
            return False

    def download_json(
        self,
        key: str,
    ) -> dict | list | None:
        """
        Download and parse JSON data from Blob Storage

        Args:
            key: Blob Storage object key (path)

        Returns:
            Parsed JSON data (dict or list) or None if not found/invalid
        """
        content = self.read_object(key, as_text=True)
        if content is None:
            return None

        try:
            return json.loads(content)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            self.logger.exception("Failed to parse JSON from {key}", key=key)
            return None
