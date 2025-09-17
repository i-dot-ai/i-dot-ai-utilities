# File Store

## Usage

### Create a FileStore object

You can create a `FileStore` object very easily by instantiating an instance of the `FileStore` abstract class using the `create_file_store` function.
The factory function `create_file_store` has been implemented to deal with circular dependency within the module.
```python
from i_dot_ai_utilities.file_store.factory import create_file_store
from i_dot_ai_utilities.file_store.types.file_store_destination_enum import FileStoreDestinationEnum
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat

def define_logger() -> StructuredLogger:
    logger_environment = ExecutionEnvironmentType.LOCAL
    logger_format = LogOutputFormat.TEXT

    return StructuredLogger(
        level="info",
        options={
            "execution_environment": logger_environment,
            "log_format": logger_format,
        },
    )


file_store = create_file_store(FileStoreDestinationEnum.AWS_S3, define_logger())

file_store.put_object("file_name.txt", "File data")
```
This is enough to initially create a file in S3 or minio. To use GCP cloud storage, or Azure blob storage,
change the `FileStoreDestinationEnum` passed to the `create` function.

<br>

***

<br>

This package takes configuration from your environment variables using pydantic-settings. The `IAI_FS_` prefix is used to allow you to configure other buckets easily.

Different groups of settings can be used for different providers.

Please set the following settings:

- **Required**
- `ENVIRONMENT: str`: The execution environment - usually `local`, `test`, `dev`, `preprod` or `prod`
- `IAI_FS_BUCKET_NAME: str`: The name of your bucket


- **AWS/minio specific**
- `IAI_FS_AWS_REGION: str`: The aws region of your S3/minio bucket
- `IAI_FS_MINIO_ADDRESS: str`: The address for minio, this is not needed when using aws
(if you're using docker-compose to run minio,
and your application is also running in docker-compose on a shared network,
please use the container name for minio here instead of `localhost`, e.g. `http://minio:9000`)
- `IAI_FS_AWS_ACCESS_KEY_ID: str`: AWS access key, generally not needed if running your
application in aws with IAM configured for the execution task
- `IAI_FS_AWS_SECRET_ACCESS_KEY: str`: AWS secret access key, generally not needed if running your
application in aws with IAM configured for the execution task


- **GCP specific**
- `IAI_FS_GCP_API_KEY: str`: The API key to use with GCP


- **Azure specific**
- `IAI_FS_AZURE_ACCOUNT_URL: str`: The account URL for Azure
- `IAI_FS_AZURE_CONNECTION_STRING: str`: The connection string to use for a connection to Azure
- `IAI_FS_AZURE_ACCOUNT_KEY: str`: The account key for Azure


- **Universal**
- `IAI_FS_DATA_DIR: str - default="app_data"`: The directory in S3/minio to store your data,
this is used to restrict user access to the root of a bucket

_Each provider can be configured independently, or you can configure all and have multiple connections at once._

<br>

***

<br>

### Important notes

> Note that errors that occur within the package will return `None` throughout, so null-handling is expected in
> consuming packages to handle errors.


> Also note that all files will be created nested inside the `IAI_FS_DATA_DIR` environment variable, which defaults to
> `app_data`. This is to support cloud-based permission models that restrict user/app access to specific dir within a bucket.
> This can be overridden to allow data to be placed at the root of the store.

<br>

***

<br>

### Supported functionality
Once the file store is initialised, you can interact with the filestore in different ways depending on your requirement.
The following methods are included, with more properties available:

#### Create object

``` python
file_store.put_object("file_name.txt", "file content")
```

#### Read object

``` python
file_store.read_object("file_name.txt")
```

#### Update object

``` python
file_store.update_object("file_name.txt", "file content updated")
```

#### Delete object

``` python
file_store.destroy_object("file_name.txt")
```

#### Check if an object exists

``` python
file_store.object_exists("file_name.txt")
```

#### Get a download link (pre-signed url)

``` python
file_store.download_object_url("file_name.txt")
```

#### List objects in bucket (limited to 1000)

``` python
file_store.list_objects()
```

#### Get object metadata

``` python
file_store.get_object_metadata()
```

#### Copy object

Note that in GCP, this will destroy and recreate the object instead of copying.
This is done to support local emulation, specifically for GCP, where copying is not supported.

``` python
file_store.copy_object("source_file_name.txt", "destination_file_name.txt")
```

#### Upload a json object

``` python
file_store.upload_json("file_name.txt", {"arg1": 1})
```

#### Download a json object

``` python
file_store.download_json("file_name.txt")
```

### Regenerating S3 boto types

Boto3 specifically suffers with a lack of `types` support. To get around this,
we built and installed the `whl` located at `../vendored` which contains a 3rd-party `types` package for boto3.
Version `1.40.31` of boto3 was used for this originally.

To update the `whl` run the following command:

``` commandline
  uvx mypy_boto3_builder ./vendored --download-static-stubs --product types-boto3-custom --output-type wheel --services s3
```

Followed by this to install them using uv:

```commandline
  uv add --dev vendored/types_boto3_custom-1.40.31-py3-none-any.whl
```
