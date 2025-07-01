import pytest
from botocore.exceptions import ClientError

from i_dot_ai_utilities.file_store.main import FileStore
from i_dot_ai_utilities.file_store.settings import Settings

file_store = FileStore()

settings = Settings()


@pytest.fixture
def client():
    return settings.boto3_client()


@pytest.fixture
def bucket(client):
    try:
        client.head_bucket(Bucket=settings.bucket_name)
    except ClientError:
        client.create_bucket(Bucket=settings.bucket_name)
    yield
    objects = client.list_objects_v2(Bucket=settings.bucket_name).get("Contents", [])
    if objects:
        objects = [{"Key": x["Key"]} for x in objects]
        client.delete_objects(Bucket=settings.bucket_name, Delete={"Objects": objects})
    client.delete_bucket(Bucket=settings.bucket_name)


@pytest.fixture
def file():
    response = file_store.create_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response
    yield
    response = file_store.delete_object("test_file.txt")
    assert response
