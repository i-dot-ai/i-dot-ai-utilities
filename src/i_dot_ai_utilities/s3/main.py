import logging
from typing import Dict, List, Optional, Union, BinaryIO
import json

import boto3
from botocore.exceptions import ClientError

from src.i_dot_ai_utilities.s3.settings import Settings

logger = logging.getLogger(__name__)
settings = Settings()


class S3Service:
    """
    S3 service class providing CRUD operations for S3 bucket objects.
    """

    def __init__(self):
        """
        Initialize S3Service with boto3 client from settings.

        Args:
            settings: Pydantic settings instance with s3_client property
        """
        self.client: boto3.client = settings.boto3_client()

    def create_object(
        self,
        key: str,
        data: Union[str, bytes, BinaryIO],
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None
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
        try:
            put_args = {
                'Bucket': bucket,
                'Key': key,
                'Body': data
            }

            if metadata:
                put_args['Metadata'] = metadata

            if content_type:
                put_args['ContentType'] = content_type

            self.client.put_object(**put_args)
            logger.info(f"Successfully uploaded object: {key} to bucket: {bucket}")
            return True

        except ClientError as e:
            logger.error(f"Failed to upload object {key}: {e}")
            return False

    def read_object(
        self,
        key: str,
        as_text: bool = False,
        encoding: str = 'utf-8'
    ) -> Optional[Union[bytes, str]]:
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
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            content = response['Body'].read()

            if as_text:
                return content.decode(encoding)
            return content

        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning(f"Object not found: {key}")
            else:
                logger.error(f"Failed to read object {key}: {e}")
            return None

    def update_object(
        self,
        key: str,
        data: Union[str, bytes, BinaryIO],
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None
    ) -> bool:
        """
        Update an existing object in S3 (same as create_object).

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
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
            logger.info(f"Successfully deleted object: {key} from bucket: {bucket}")
            return True

        except ClientError as e:
            logger.error(f"Failed to delete object {key}: {e}")
            return False

    def object_exists(self, key: str) -> bool:
        """
        Check if an object exists in S3.

        Args:
            key: S3 object key (path)

        Returns:
            bool: True if object exists, False otherwise
        """
        bucket = settings.bucket_name
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            logger.error(f"Error checking object existence {key}: {e}")
            return False

    def list_objects(
        self,
        prefix: str = "",
        max_keys: int = 1000
    ) -> List[Dict[str, Union[str, int]]]:
        """
        List objects in S3 bucket with optional prefix filter.

        Args:
            prefix: Optional prefix to filter objects
            max_keys: Maximum number of objects to return

        Returns:
            List of dictionaries containing object information
        """
        bucket = settings.bucket_name
        try:
            response = self.client.list_objects_v2(
                Bucket=bucket,
                Prefix=prefix,
                MaxKeys=max_keys
            )

            objects = []
            for obj in response.get('Contents', []):
                objects.append({
                    'key': obj['Key'],
                    'size': obj['Size'],
                    'last_modified': obj['LastModified'].isoformat(),
                    'etag': obj['ETag'].strip('"')
                })

            return objects

        except ClientError as e:
            logger.error(f"Failed to list objects with prefix {prefix}: {e}")
            return []

    def get_object_metadata(
        self,
        key: str,
    ) -> Optional[Dict[str, Union[str, int]]]:
        """
        Get metadata for an S3 object.

        Args:
            key: S3 object key (path)

        Returns:
            Dictionary containing object metadata or None if not found
        """
        bucket = settings.bucket_name
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
            return {
                'content_length': response['ContentLength'],
                'content_type': response.get('ContentType', ''),
                'last_modified': response['LastModified'].isoformat(),
                'etag': response['ETag'].strip('"'),
                'metadata': response.get('Metadata', {})
            }

        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                logger.warning(f"Object not found: {key}")
            else:
                logger.error(f"Failed to get metadata for {key}: {e}")
            return None

    def copy_object(
        self,
        source_key: str,
        dest_key: str,
    ) -> bool:
        """
        Copy an object within S3.

        Args:
            source_key: Source S3 object key
            dest_key: Destination S3 object key

        Returns:
            bool: True if successful, False otherwise
        """
        bucket = settings.bucket_name

        try:
            copy_source = {'Bucket': bucket, 'Key': source_key}
            self.client.copy_object(
                CopySource=copy_source,
                Bucket=bucket,
                Key=dest_key
            )
            logger.info(f"Successfully copied {source_key} to {dest_key}")
            return True

        except ClientError as e:
            logger.error(f"Failed to copy object {source_key} to {dest_key}: {e}")
            return False

    def upload_json(
        self,
        key: str,
        data: Union[dict, list],
        metadata: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        Upload JSON data to S3.

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
                content_type='application/json'
            )
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize data as JSON: {e}")
            return False

    def download_json(
        self,
        key: str,
    ) -> Optional[Union[dict, list]]:
        """
        Download and parse JSON data from S3.

        Args:
            key: S3 object key (path)
            bucket: S3 bucket name (uses default if not provided)

        Returns:
            Parsed JSON data (dict or list) or None if not found/invalid
        """
        content = self.read_object(key, as_text=True)
        if content is None:
            return None

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from {key}: {e}")
            return None


# Example usage:
"""
from your_app.config import settings
from your_app.services.s3_service import S3Service

# Initialize service
s3_service = S3Service(settings)

# Create/upload
s3_service.create_object('path/to/file.txt', 'Hello World')
s3_service.upload_json('data/config.json', {'key': 'value'})

# Read/download
content = s3_service.read_object('path/to/file.txt', as_text=True)
config = s3_service.download_json('data/config.json')

# Update (same as create)
s3_service.update_object('path/to/file.txt', 'Updated content')

# Delete
s3_service.delete_object('path/to/file.txt')

# Check existence
exists = s3_service.object_exists('path/to/file.txt')

# List objects
objects = s3_service.list_objects(prefix='data/')

# Get metadata
metadata = s3_service.get_object_metadata('path/to/file.txt')

# Copy object
s3_service.copy_object('source/file.txt', 'dest/file.txt')
"""
