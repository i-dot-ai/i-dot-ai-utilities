import json
from typing import TYPE_CHECKING, Any, BinaryIO, Unpack

if TYPE_CHECKING:
    from types_boto3_s3.type_defs import CopySourceTypeDef

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from types_boto3_s3 import S3Client

from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings
from i_dot_ai_utilities.file_store.types.kwargs_dicts import S3ClientKwargs
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


class S3FileStore(FileStore):
    """
    File storage class providing CRUD operations for S3 bucket objects in AWS S3 and minio
    """

    def __init_boto3_client(self, **kwargs: Unpack[S3ClientKwargs]) -> S3Client:
        """
        This function returns the client connection to S3 or minio using boto3,
        depending on the environment variable `ENVIRONMENT`
        :return: Boto3 client with an S3 session to either minio or AWS s3
        """
        if self.settings.environment.lower() in ["local", "test"]:
            # Filter out any conflicting kwargs that we're setting explicitly
            filtered_kwargs = {
                k: v
                for k, v in kwargs.items()
                if k not in ["endpoint_url", "aws_access_key_id", "aws_secret_access_key", "config"]
            }

            return boto3.client(  # type: ignore[misc]
                "s3",
                endpoint_url=self.settings.minio_address,
                aws_access_key_id=self.settings.aws_access_key_id,
                aws_secret_access_key=self.settings.aws_secret_access_key,  # pragma: allowlist secret
                config=Config(signature_version="s3v4"),
                **filtered_kwargs,  # type: ignore[arg-type]
            )
        else:
            # Filter out profile_name which isn't valid for session.client
            client_kwargs = {k: v for k, v in kwargs.items() if k != "profile_name"}
            session = boto3.Session(self.settings.aws_region)
            return session.client("s3", **client_kwargs)  # type: ignore[misc, arg-type]

    def __init__(self, logger: StructuredLogger, settings: Settings, **kwargs: Unpack[S3ClientKwargs]) -> None:
        """
        Initialize FileStore with boto3 client from settings
        :param logger: A `StructuredLogger` instance
        """
        self.logger = logger
        self.settings = settings
        self.client: S3Client = self.__init_boto3_client(**kwargs)

    def __prefix_key(self, key: str) -> str:
        """
        Returns the key with a prefix if it's set
        :param key: The S3 object key
        :return: The key with a prefix if it's set
        """
        return key if not self.settings.data_dir else f"{self.settings.data_dir}/{key}"

    def get_client(self) -> S3Client:
        return self.client

    def put_object(
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
        bucket = self.settings.bucket_name
        key = self.__prefix_key(key)
        try:
            # Use explicit arguments instead of **kwargs to satisfy mypy
            if metadata and content_type:
                self.client.put_object(Bucket=bucket, Key=key, Body=data, Metadata=metadata, ContentType=content_type)
            elif metadata:
                self.client.put_object(Bucket=bucket, Key=key, Body=data, Metadata=metadata)
            elif content_type:
                self.client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
            else:
                self.client.put_object(Bucket=bucket, Key=key, Body=data)

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
        bucket = self.settings.bucket_name
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
        return self.put_object(key, data, metadata, content_type)

    def delete_object(self, key: str) -> bool:
        """
        Delete an object from S3.

        Args:
            key: S3 object key (path)

        Returns:
            bool: True if successful, False otherwise
        """
        bucket = self.settings.bucket_name
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
        bucket = self.settings.bucket_name
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

    def download_object_url(self, key: str, expiration: int = 3600) -> str | None:
        """
        Get an objects pre-signed URL

        Args:
            key: S3 object key (path)
            expiration: Expiration time in seconds
        Returns:
            str: S3 object pre-signed URL as string. If error, returns None
        """
        bucket = self.settings.bucket_name
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
        bucket = self.settings.bucket_name
        prefix = self.__prefix_key(prefix)
        objects = []
        try:
            response = self.client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
            for obj in response.get("Contents", []):
                objects.append(
                    {
                        "key": str(obj["Key"]),
                        "size": int(obj["Size"]),
                        "last_modified": obj["LastModified"].isoformat(),
                        "etag": str(obj["ETag"]).strip('"'),
                    }
                )
        except ClientError:
            self.logger.exception("Failed to list objects with prefix {prefix}", prefix=prefix)
            return []
        else:
            return objects  # type: ignore[return-value]

    def get_object_metadata(
        self,
        key: str,
    ) -> dict[str, str | int | dict[str, Any]] | None:
        """
        Get metadata for an S3 object

        Args:
            key: S3 object key (path)

        Returns:
            Dictionary containing object metadata or None if not found
        """
        bucket = self.settings.bucket_name
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
        bucket = self.settings.bucket_name
        source_key = self.__prefix_key(source_key)
        dest_key = self.__prefix_key(dest_key)
        try:
            copy_source: CopySourceTypeDef = {"Bucket": bucket, "Key": source_key}
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
