from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All environment settings required here are for S3 access.

    All environment settings except for `environment` should be prefixed with `IAI_FS_`

    The following environment variables are expected:

    - **ENVIRONMENT**: Accepted values are `local`, `test`, `dev`, `preprod`, and `prod`
    - **IAI_FS_BUCKET_NAME**: The name of the bucket to interact with
    - **IAI_FS_AWS_REGION**: The AWS region to interact with
    - **IAI_MINIO_ADDRESS**: The minio host address (for localhost,
    only needs setting if not using the default `localhost` minio address)
    - **IAI_FS_AWS_ACCESS_KEY_ID**: The AWS access key ID (for localhost,
    only needs setting if not using the default minio credentials)
    - **IAI_FS_AWS_SECRET_ACCESS_KEY**: The AWS secret key (for localhost,
    only needs setting if not using the default minio credentials)
    - **IAI_FS_GCP_API_KEY**: The API key to use for GCP access
    - **IAI_FS_AZURE_ACCOUNT_URL**: The Azure account URL
    - **IAI_FS_AZURE_CONNECTION_STRING**: The Azure connection string
    - **IAI_FS_AZURE_ACCOUNT_KEY**: The Azure account key
    - **IAI_DATA_DIR**: The data directory to use inside the set S3 bucket
    (defaults to `app_data`)

    """

    environment: str = Field(alias="ENVIRONMENT")
    bucket_name: str = Field()
    aws_region: str | None = Field()
    data_dir: str = Field(default="app_data")
    minio_address: str | None = Field(default=None)
    aws_access_key_id: str | None = Field(default=None)
    aws_secret_access_key: str | None = Field(default=None)
    gcp_api_key: str | None = Field(default=None)
    azure_account_url: str | None = Field(default=None)
    azure_connection_string: str | None = Field(default=None)
    azure_account_key: str | None = Field(default=None)

    model_config = SettingsConfigDict(extra="ignore", env_prefix="IAI_FS_", case_sensitive=False)
