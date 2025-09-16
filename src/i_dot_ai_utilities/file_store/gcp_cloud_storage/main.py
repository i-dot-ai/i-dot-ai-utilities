import json
import os
from datetime import timedelta
from typing import Any, BinaryIO

from google.cloud import storage
from google.cloud.exceptions import GoogleCloudError, NotFound
from typing_extensions import Unpack

from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.file_store.types.kwargs_dicts import GCPClientKwargs
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


class GCPFileStore(FileStore):
    """
    File storage class providing CRUD operations for GCP Cloud Storage objects
    """

    def __init_gcp_client(self, **kwargs: Unpack[GCPClientKwargs]) -> storage.Client:
        """
        This function returns the client connection to GCP Cloud Storage
        :return: GCP Cloud Storage client
        """
        if self.settings.environment.lower() in ["local", "test"]:
            # Set emulator host for local testing
            os.environ["STORAGE_EMULATOR_HOST"] = "http://localhost:9023"
            return storage.Client(
                project="test-project",  # Fake project for emulator
                **kwargs,
            )
        else:
            return storage.Client(**kwargs)

    def __init__(self, logger: StructuredLogger, settings: Settings, **kwargs: Unpack[GCPClientKwargs]) -> None:
        """
        Initialize FileStore with GCP Cloud Storage client from settings
        :param logger: A `StructuredLogger` instance
        :param settings: Settings instance containing configuration
        :param kwargs: Additional GCP client configuration parameters
        """
        self.logger = logger
        self.settings = settings
        self.client: storage.Client = self.__init_gcp_client(**kwargs)
        self.bucket = self.client.bucket(self.settings.bucket_name)

    def __prefix_key(self, key: str) -> str:
        """
        Returns the key with a prefix if it's set
        :param key: The Cloud Storage object key
        :return: The key with a prefix if it's set
        """
        return key if not self.settings.data_dir else f"{self.settings.data_dir}/{key}"

    def get_client(self) -> storage.Client:
        return self.client

    def put_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        """
        Create/upload an object to Cloud Storage.

        Args:
            key: Cloud Storage object key (path)
            data: Data to upload (string, bytes, or file-like object)
            metadata: Optional metadata dictionary
            content_type: Optional content type

        Returns:
            bool: True if successful, False otherwise
        """
        bucket_name = self.settings.bucket_name
        key = self.__prefix_key(key)
        try:
            blob = self.bucket.blob(key)

            if metadata:
                blob.metadata = metadata

            if content_type:
                blob.content_type = content_type

            if isinstance(data, str):
                blob.upload_from_string(data)
            elif hasattr(data, "read"):
                blob.upload_from_file(data)
            else:
                blob.upload_from_string(data)

            self.logger.info("Successfully uploaded object: {key} to bucket: {bucket}", key=key, bucket=bucket_name)
        except GoogleCloudError:
            self.logger.exception("Failed to upload object {key}", key=key)
            return False
        else:
            return True

    def read_object(self, key: str, as_text: bool = False, encoding: str = "utf-8") -> bytes | str | None:
        """
        Read/download an object from Cloud Storage.

        Args:
            key: Cloud Storage object key (path)
            as_text: If True, return as string, otherwise as bytes
            encoding: Text encoding if as_text is True

        Returns:
            Object content as bytes or string, None if not found
        """
        key = self.__prefix_key(key)
        try:
            blob = self.bucket.blob(key)
            if as_text:
                content: str = blob.download_as_text(encoding=encoding)
                return content
            else:
                content_bytes: bytes = blob.download_as_bytes()
                return content_bytes
        except NotFound:
            self.logger.warning("Object not found: {key}", key=key)
            return None
        except GoogleCloudError:
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
        Update an existing object in Cloud Storage (same as create_object)

        Args:
            key: Cloud Storage object key (path)
            data: New data to upload
            metadata: Optional metadata dictionary
            content_type: Optional content type

        Returns:
            bool: True if successful, False otherwise
        """
        return self.put_object(key, data, metadata, content_type)

    def delete_object(self, key: str) -> bool:
        """
        Delete an object from Cloud Storage.

        Args:
            key: Cloud Storage object key (path)

        Returns:
            bool: True if successful, False otherwise
        """
        bucket_name = self.settings.bucket_name
        key = self.__prefix_key(key)
        try:
            blob = self.bucket.blob(key)
            blob.delete()
            self.logger.info("Successfully deleted object: {key} from bucket: {bucket}", key=key, bucket=bucket_name)
        except NotFound:
            self.logger.warning("Object not found for deletion: {key}", key=key)
            return False
        except GoogleCloudError:
            self.logger.exception("Failed to delete object {key}", key=key)
            return False
        else:
            return True

    def object_exists(self, key: str) -> bool:
        """
        Check if an object exists in Cloud Storage

        Args:
            key: Cloud Storage object key (path)

        Returns:
            bool: True if object exists, False otherwise
        """
        key = self.__prefix_key(key)
        try:
            blob = self.bucket.blob(key)
            blob_exists: bool = blob.exists()
        except GoogleCloudError:
            self.logger.exception("Error checking object {key} existence", key=key)
            return False
        else:
            return blob_exists

    def download_object_url(self, key: str, expiration: int = 3600) -> str | None:
        """
        Get an objects pre-signed URL

        Args:
            key: Cloud Storage object key (path)
            expiration: Expiration time in seconds
        Returns:
            str: Cloud Storage object pre-signed URL as string. If error, returns None
        """
        try:
            if not self.object_exists(key):
                return None

            blob = self.bucket.blob(self.__prefix_key(key))

            if self.settings.environment.lower() in ["local", "test"]:
                # For testing, return a simple URL since signed URLs don't work without proper creds
                base_url = os.getenv("STORAGE_EMULATOR_HOST", "http://localhost:9023")
                return f"{base_url}/{self.settings.bucket_name}/{self.__prefix_key(key)}"

            url = blob.generate_signed_url(expiration=timedelta(seconds=expiration), method="GET")
            return str(url)
        except GoogleCloudError:
            self.logger.exception("Error generating signed URL for {key}", key=self.__prefix_key(key))
            return None

    def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict[str, str | int]]:
        """
        List objects in Cloud Storage bucket with optional prefix filter

        Args:
            prefix: Optional prefix to filter objects
            max_keys: Maximum number of objects to return

        Returns:
            List of dictionaries containing object information
        """
        prefix = self.__prefix_key(prefix)
        objects = []
        try:
            blobs = self.client.list_blobs(self.bucket, prefix=prefix, max_results=max_keys)
            for blob in blobs:
                objects.append(
                    {
                        "key": blob.name,
                        "size": blob.size or 0,
                        "last_modified": blob.time_created.isoformat() if blob.time_created else "",
                        "etag": blob.etag or "",
                    }
                )
        except GoogleCloudError:
            self.logger.exception("Failed to list objects with prefix {prefix}", prefix=prefix)
            return []
        else:
            return objects

    def get_object_metadata(
        self,
        key: str,
    ) -> dict[str, str | int | dict[str, Any]] | None:
        """
        Get metadata for a Cloud Storage object

        Args:
            key: Cloud Storage object key (path)

        Returns:
            Dictionary containing object metadata or None if not found
        """
        key = self.__prefix_key(key)
        try:
            blob = self.bucket.blob(key)
            blob.reload()

            return {
                "content_length": blob.size or 0,
                "content_type": blob.content_type or "",
                "last_modified": blob.time_created.isoformat() if blob.time_created else "",
                "etag": blob.etag or "",
                "metadata": blob.metadata or {},
            }

        except NotFound:
            self.logger.warning("Object not found: {key}", key=key)
            return None
        except GoogleCloudError:
            self.logger.exception("Failed to get metadata for {key}", key=key)
            return None

    def copy_object(
        self,
        source_key: str,
        dest_key: str,
    ) -> bool:
        """
        Copy an object within Cloud Storage

        Args:
            source_key: Source Cloud Storage object key
            dest_key: Destination Cloud Storage object key

        Returns:
            bool: True if successful, False otherwise
        """
        source_key = self.__prefix_key(source_key)
        dest_key = self.__prefix_key(dest_key)
        try:
            # Workaround for gcsemulator not supporting copyTo operation
            # Read the source object and write it as destination
            source_blob = self.bucket.blob(source_key)
            if not source_blob.exists():
                self.logger.warning("Source object not found: {source_key}", source_key=source_key)
                return False

            source_data = source_blob.download_as_bytes()
            source_metadata = source_blob.metadata or {}
            source_content_type = source_blob.content_type

            dest_blob = self.bucket.blob(dest_key)
            if source_metadata:
                dest_blob.metadata = source_metadata
            if source_content_type:
                dest_blob.content_type = source_content_type

            dest_blob.upload_from_string(source_data)

        except GoogleCloudError:
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
        Upload JSON data to Cloud Storage

        Args:
            key: Cloud Storage object key (path)
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
        Download and parse JSON data from Cloud Storage

        Args:
            key: Cloud Storage object key (path)

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
