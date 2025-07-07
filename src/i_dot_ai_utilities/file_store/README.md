# File Store

## Usage

### Create a FileStore object

You can create a `FileStore` object very easily by instantiating an instance of the `FileStore` class:
```python
from i_dot_ai_utilities.file_store import FileStore

file_store = FileStore()

file_store.create_object("file_name.txt", "File data")
```
This is enough to initially create a file in S3 or minio.

<br>

***

<br>

This package takes configuration from your environment variables using pydantic-settings. The `IAI_FS_` prefix is used to allow you to configure other buckets easily.

Please set the following settings:

- `ENVIRONMENT: str`: The execution environment - usually `local`, `test`, `dev`, `preprod` or `prod`
- `IAI_FS_BUCKET_NAME: str`: The name of your S3/minio bucket
- `IAI_FS_AWS_REGION: str`: The aws region of your S3/minio bucket
- `IAI_FS_MINIO_ADDRESS: str - default="http://localhost:9000"`: The address for minio, this is not needed when using aws
(if you're using docker-compose to run minio,
and your application is also running in docker-compose on a shared network,
please use the container name for minio here instead of `localhost`, e.g. `http://minio:9000`)
- `IAI_FS_AWS_ACCESS_KEY_ID: str - default="minioadmin"`: AWS access key, generally not needed if running your
application in aws with IAM configured for the execution task
- `IAI_FS_AWS_SECRET_ACCESS_KEY: str - default="minioadmin"`: AWS secret access key, generally not needed if running your
application in aws with IAM configured for the execution task
- `IAI_FS_DATA_DIR: str - default="app_data"`: The directory in S3/minio to store your data,
this is used to restrict user access to the root of a bucket

<br>

***

<br>

### Important notes

> Note that errors that occur within the package will return `None` throughout, so null-handling is expected in
> consuming packages to handle errors.


> Also note that all files will be created nested inside the `IAI_FS_DATA_DIR` environment variable, which defaults to
> `app_data`. This is to support IAM permissions that restrict user/app access to specific dir within a bucket.

<br>

***

<br>

### Supported functionality
Once the file store is initialised, you can interact with S3/minio in different ways depending on your requirement.
The following methods are included, with more properties available:

#### Create object

``` python
file_store.create_object("file_name.txt", "file content")
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
file_store.get_presigned_url("file_name.txt")
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
