from enum import Enum


class FileStoreDestinationEnum(Enum):
    AWS_S3 = 0
    GCP_CLOUD_STORAGE = 1
    AZURE_BLOB_STORAGE = 2
