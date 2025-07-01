import boto3
from botocore.config import Config
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
    - **IAI_DATA_DIR**: The data directory to use inside the set S3 bucket
    (defaults to `app_data`)

    """

    environment: str = Field(validation_alias="ENVIRONMENT")
    bucket_name: str = Field()
    aws_region: str = Field()
    minio_address: str = Field(default="http://localhost:9000")
    aws_access_key_id: str = Field(default="minioadmin")
    aws_secret_access_key: str = Field(default="minioadmin")
    data_dir: str = Field(default="app_data")

    model_config = SettingsConfigDict(extra="ignore", env_prefix="IAI_FS_")

    def boto3_client(self) -> boto3.client:
        """
        This function returns the client connection to S3 or minio using boto3,
        depending on the environment variable `ENVIRONMENT`
        :return: Boto3 client with an S3 session to either minio or AWS s3
        """
        if self.environment.lower() in ["local", "test"]:
            return boto3.client(
                "s3",
                endpoint_url=self.minio_address,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,  # pragma: allowlist secret
                config=Config(signature_version="s3v4"),
            )
        elif self.environment.lower() in ["production", "preprod", "dev"]:
            session = boto3.Session(self.aws_region)
            return session.client("s3")
        else:
            return None
