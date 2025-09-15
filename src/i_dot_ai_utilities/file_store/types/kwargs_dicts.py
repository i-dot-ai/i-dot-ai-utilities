from typing import Any, TypedDict

from botocore.config import Config
from google.auth.credentials import Credentials


class GCPClientKwargs(TypedDict, total=False):
    """TypedDict for GCP Storage Client initialization parameters"""

    project: str | None
    credentials: Credentials | None
    client_info: object | None
    client_options: object | None
    quota_project_id: str | None
    api_key: str | None


class AzureClientKwargs(TypedDict, total=False):
    """TypedDict for Azure Blob Storage Client initialization parameters"""

    api_version: str | None
    secondary_hostname: str | None
    max_block_size: int | None
    max_single_put_size: int | None
    max_page_size: int | None
    max_single_get_size: int | None
    max_chunk_get_size: int | None
    connection_timeout: int | None
    read_timeout: int | None
    retry_total: int | None
    retry_connect: int | None
    retry_read: int | None
    retry_status: int | None
    retry_to_secondary: bool | None
    location_mode: str | None
    host_base: str | None
    protocol: str | None
    endpoint_suffix: str | None
    custom_domain: str | None
    request_session: Any | None
    socket_timeout: int | None
    token_refresh_retry_total: int | None
    token_refresh_retry_backoff_factor: float | None


class S3ClientKwargs(TypedDict, total=False):
    """TypedDict for S3 boto3 client initialization parameters"""

    region_name: str | None
    api_version: str | None
    use_ssl: bool | None
    verify: bool | None
    endpoint_url: str | None
    aws_access_key_id: str | None
    aws_secret_access_key: str | None
    aws_session_token: str | None
    config: Config | None
    profile_name: str | None
    role_arn: str | None
    role_session_name: str | None
    external_id: str | None
    mfa_serial: str | None
    mfa_token: str | None
    duration_seconds: int | None
