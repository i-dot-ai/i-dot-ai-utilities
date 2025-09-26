from typing import Any

import pytest

from i_dot_ai_utilities.file_store.main import FileStore


@pytest.mark.usefixtures("boto3_client", "bucket")
def test_create_file(s3_file_store: FileStore) -> None:
    response = s3_file_store.put_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_read_file(s3_file_store: FileStore) -> None:
    response = s3_file_store.read_object("test_file.txt", True)
    assert response == "file_content"


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_copy_object(s3_file_store: FileStore) -> None:
    copy_response = s3_file_store.copy_object("test_file.txt", "test_file2.txt")
    assert copy_response

    read_response: bytes | str | None = s3_file_store.read_object("test_file2.txt", as_text=True)
    assert read_response == "file_content"


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_list_objects(s3_file_store: FileStore) -> None:
    copy_response = s3_file_store.copy_object("test_file.txt", "test_file2.txt")
    assert copy_response

    list_response: list[dict[str, str | int]] = s3_file_store.list_objects("test")
    file_keys = [r["key"] for r in list_response]
    assert file_keys == ["app_data/test_file.txt", "app_data/test_file2.txt"]


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_delete_object(s3_file_store: FileStore) -> None:
    response = s3_file_store.delete_object("test_file.txt")
    assert response


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_object_metadata(s3_file_store: FileStore) -> None:
    response: dict = s3_file_store.get_object_metadata("test_file.txt")  # type: ignore[assignment]
    assert response["metadata"] == {"metadata": "metadata"}


@pytest.mark.usefixtures("boto3_client", "bucket")
def test_json_upload_file(s3_file_store: FileStore) -> None:
    file_json_content = {"file_content": "json_content"}

    upload_response = s3_file_store.upload_json("test_file.txt", file_json_content)
    assert upload_response

    read_response = s3_file_store.read_object("test_file.txt", True)
    assert read_response == '{\n  "file_content": "json_content"\n}'


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_update_object(s3_file_store: FileStore) -> None:
    update_response = s3_file_store.update_object(
        "test_file.txt", "file_content but updated", metadata={"metadata": "metadata"}
    )
    assert update_response

    read_response = s3_file_store.read_object("test_file.txt", True)
    assert read_response == "file_content but updated"


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_get_pre_signed_url(s3_file_store: FileStore) -> None:
    response: str | None = s3_file_store.download_object_url("test_file.txt")
    assert response
    assert response.startswith("http://localhost:9000/test-bucket/app_data/test_file.txt?X-Amz-Algorithm")


@pytest.mark.usefixtures("boto3_client", "bucket", "file")
def test_object_exists(s3_file_store: FileStore) -> None:
    response = s3_file_store.object_exists("test_file.txt")
    assert response


@pytest.mark.usefixtures("boto3_client", "bucket")
def test_file_doesnt_exist(s3_file_store: FileStore) -> None:
    response = s3_file_store.list_objects("test")
    file_keys = [r["key"] for r in response]
    assert "app_data/test_file5.txt" not in file_keys


@pytest.mark.usefixtures("boto3_client", "bucket")
def test_get_none_pre_signed_url(s3_file_store: FileStore) -> None:
    response = s3_file_store.download_object_url("test_file6.txt")
    assert response is None


@pytest.mark.usefixtures("boto3_client", "bucket")
def test_get_empty_json_object(s3_file_store: FileStore) -> None:
    create_response = s3_file_store.put_object("test_file.txt", "")
    assert create_response

    download_response: dict[Any, Any] | list[Any] | None = s3_file_store.download_json("test_file.txt")
    assert download_response is None
