import pytest

from i_dot_ai_utilities.file_store.main import FileStore

file_store = FileStore()


@pytest.mark.order(1)
@pytest.mark.usefixtures("client", "bucket")
def test_create_file() -> None:
    response = file_store.create_object("test_file.txt", "file_content", metadata={"metadata": "metadata"})
    assert response


@pytest.mark.order(2)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_read_file() -> None:
    response = file_store.read_object("test_file.txt", True)
    assert response == "file_content"


@pytest.mark.order(3)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_copy_object() -> None:
    response = file_store.copy_object("test_file.txt", "test_file2.txt")
    assert response

    response = file_store.read_object("test_file2.txt", True)
    assert response == "file_content"


@pytest.mark.order(4)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_list_objects() -> None:
    response = file_store.copy_object("test_file.txt", "test_file2.txt")
    assert response

    response = file_store.list_objects("test")
    file_keys = [r["key"] for r in response]
    assert file_keys == ["app_data/test_file.txt", "app_data/test_file2.txt"]


@pytest.mark.order(5)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_delete_object() -> None:
    response = file_store.delete_object("test_file.txt")
    assert response


@pytest.mark.order(6)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_object_metadata() -> None:
    response = file_store.get_object_metadata("test_file.txt")
    assert response["metadata"] == {"metadata": "metadata"}


@pytest.mark.order(7)
@pytest.mark.usefixtures("client", "bucket")
def test_json_upload_file() -> None:
    file_json_content = {"file_content": "json_content"}

    response = file_store.upload_json("test_file.txt", file_json_content)
    assert response

    response = file_store.read_object("test_file.txt", True)
    assert response == '{\n  "file_content": "json_content"\n}'


@pytest.mark.order(8)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_update_object() -> None:
    response = file_store.update_object("test_file.txt", "file_content but updated", metadata={"metadata": "metadata"})
    assert response

    response = file_store.read_object("test_file.txt", True)
    assert response == "file_content but updated"


@pytest.mark.order(9)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_get_pre_signed_url() -> None:
    response = file_store.get_pre_signed_url("test_file.txt")
    assert response.startswith("http://localhost:9000/test-bucket/app_data/test_file.txt?X-Amz-Algorithm")


@pytest.mark.order(10)
@pytest.mark.usefixtures("client", "bucket", "file")
def test_object_exists() -> None:
    response = file_store.object_exists("test_file.txt")
    assert response


@pytest.mark.order(11)
@pytest.mark.usefixtures("client", "bucket")
def test_file_doesnt_exist() -> None:
    response = file_store.list_objects("test")
    file_keys = [r["key"] for r in response]
    assert "app_data/test_file5.txt" not in file_keys


@pytest.mark.order(12)
@pytest.mark.usefixtures("client", "bucket")
def test_get_none_pre_signed_url() -> None:
    response = file_store.get_pre_signed_url("test_file6.txt")
    assert response is None


@pytest.mark.order(13)
@pytest.mark.usefixtures("client", "bucket")
def test_get_empty_json_object() -> None:
    response = file_store.create_object("test_file.txt", "")
    assert response

    response = file_store.download_json("test_file.txt")
    assert response is None
