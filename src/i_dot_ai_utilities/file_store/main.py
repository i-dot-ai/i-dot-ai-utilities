import json
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    import boto3
from botocore.exceptions import ClientError

from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger

settings = Settings()  # type: ignore[call-arg]


class FileStore:
    """
    File storage class providing CRUD operations for S3 bucket objects in AWS S3 and minio
    """

    def __init__(self, logger: StructuredLogger) -> None:
        """
        Initialize FileStore with boto3 client from settings
        :param logger: A `StructuredLogger` instance
        """
        self.client: boto3.client = settings.boto3_client()
        self.logger = logger

    @staticmethod
    def __prefix_key(key: str) -> str:
        """
        Returns the key with a prefix if it's set
        :param key: The S3 object key
        :return: The key with a prefix if it's set
        """
        return key if not settings.data_dir else f"{settings.data_dir}/{key}"

    def create_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        """
        Create/upload an object to S3.

        Args:
            key: S3 object key (path)
            data: Data to upload (string, bytes, or file-like object)
            metadata: Optional metadata dictionary
            content_type: Optional content type

        Returns:
            bool: True if successful, False otherwise
        """
        bucket = settings.bucket_name
        key = self.__prefix_key(key)
        try:
            put_args = {"Bucket": bucket, "Key": key, "Body": data}

            if metadata:
                put_args["Metadata"] = metadata  # type: ignore[assignment]

            if content_type:
                put_args["ContentType"] = content_type

            self.client.put_object(**put_args)
            self.logger.info("Successfully uploaded object: {key} to bucket: {bucket}", key=key, bucket=bucket)
        except ClientError:
            self.logger.exception("Failed to upload object {key}", key=key)
            return False
        else:
            return True

    def read_object(self, key: str, as_text: bool = False, encoding: str = "utf-8") -> bytes | str | None:
        """
        Read/download an object from S3.

        Args:
            key: S3 object key (path)
            as_text: If True, return as string, otherwise as bytes
            encoding: Text encoding if as_text is True

        Returns:
            Object content as bytes or string, None if not found
        """
        bucket = settings.bucket_name
        key = self.__prefix_key(key)
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            content: bytes | str | None = response["Body"].read()
        except ClientError as exception:
            if exception.response["Error"]["Code"] == "NoSuchKey":
                self.logger.warning("Object not found: {key}", key=key)
            else:
                self.logger.exception("Failed to read object {key}", key=key)
            return None
        else:
            if as_text and type(content) is bytes:
                result: str = content.decode(encoding)
                return result
            return content

    def update_object(
        self,
        key: str,
        data: str | bytes | BinaryIO,
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> bool:
        """
        Update an existing object in S3 (same as create_object)

        Args:
            key: S3 object key (path)
            data: New data to upload
            metadata: Optional metadata dictionary
            content_type: Optional content type

        Returns:
            bool: True if successful, False otherwise
        """
        return self.create_object(key, data, metadata, content_type)

    def delete_object(self, key: str) -> bool:
        """
        Delete an object from S3.

        Args:
            key: S3 object key (path)

        Returns:
            bool: True if successful, False otherwise
        """
        bucket = settings.bucket_name
        key = self.__prefix_key(key)
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
            self.logger.info("Successfully deleted object: {key} from bucket: {bucket}", key=key, bucket=bucket)
        except ClientError:
            self.logger.exception("Failed to delete object {key}", key=key)
            return False
        else:
            return True

    def object_exists(self, key: str) -> bool:
        """
        Check if an object exists in S3

        Args:
            key: S3 object key (path)

        Returns:
            bool: True if object exists, False otherwise
        """
        bucket = settings.bucket_name
        key = self.__prefix_key(key)
        try:
            self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as exception:
            if exception.response["Error"]["Code"] == "404":
                return False
            self.logger.exception("Error checking object {key} existence", key=key)
            return False
        else:
            return True

    def get_pre_signed_url(self, key: str, expiration: int = 3600) -> str | None:
        """
        Get an objects pre-signed URL

        Args:
            key: S3 object key (path)
            expiration: Expiration time in seconds
        Returns:
            str: S3 object pre-signed URL as string. If error, returns None
        """
        bucket = settings.bucket_name
        try:
            does_object_exist = self.object_exists(key)
            if not does_object_exist:
                return None
            return str(
                self.client.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": self.__prefix_key(key)}, ExpiresIn=expiration
                )
            )
        except ClientError as exception:
            if exception.response["Error"]["Code"] == "404":
                return None
            self.logger.exception("Error checking object existence {key}", key=self.__prefix_key(key))
            return None

    def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[dict[str, str | int]]:
        """
        List objects in S3 bucket with optional prefix filter

        Args:
            prefix: Optional prefix to filter objects
            max_keys: Maximum number of objects to return

        Returns:
            List of dictionaries containing object information
        """
        bucket = settings.bucket_name
        prefix = self.__prefix_key(prefix)
        objects = []
        try:
            response = self.client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
            for obj in response.get("Contents", []):
                objects.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                        "etag": obj["ETag"].strip('"'),
                    }
                )
        except ClientError:
            self.logger.exception("Failed to list objects with prefix {prefix}", prefix=prefix)
            return []
        else:
            return objects

    def get_object_metadata(
        self,
        key: str,
    ) -> dict[str, str | int] | None:
        """
        Get metadata for an S3 object

        Args:
            key: S3 object key (path)

        Returns:
            Dictionary containing object metadata or None if not found
        """
        bucket = settings.bucket_name
        key = self.__prefix_key(key)
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
            return {
                "content_length": response["ContentLength"],
                "content_type": response.get("ContentType", ""),
                "last_modified": response["LastModified"].isoformat(),
                "etag": response["ETag"].strip('"'),
                "metadata": response.get("Metadata", {}),
            }

        except ClientError as exception:
            if exception.response["Error"]["Code"] == "404":
                self.logger.warning("Object not found: {key}", key=key)
            else:
                self.logger.exception("Failed to get metadata for {key}", key=key)
            return None

    def copy_object(
        self,
        source_key: str,
        dest_key: str,
    ) -> bool:
        """
        Copy an object within S3

        Args:
            source_key: Source S3 object key
            dest_key: Destination S3 object key

        Returns:
            bool: True if successful, False otherwise
        """
        bucket = settings.bucket_name
        source_key = self.__prefix_key(source_key)
        dest_key = self.__prefix_key(dest_key)
        try:
            copy_source = {"Bucket": bucket, "Key": source_key}
            self.client.copy_object(CopySource=copy_source, Bucket=bucket, Key=dest_key)
        except ClientError:
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
        Upload JSON data to S3

        Args:
            key: S3 object key (path)
            data: Dictionary or list to serialize as JSON
            metadata: Optional metadata dictionary

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            json_data = json.dumps(data, indent=2)
            return self.create_object(
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
        Download and parse JSON data from S3

        Args:
            key: S3 object key (path)

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
