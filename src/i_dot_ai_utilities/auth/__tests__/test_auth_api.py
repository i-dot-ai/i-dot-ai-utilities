# mypy: disable-error-code="no-untyped-def"

from unittest.mock import patch

import pytest
import requests

from i_dot_ai_utilities.auth.__tests__.conftest import (
    get_mock_async_client,
    get_mock_httpx_response,
    get_mock_requests_response,
)
from i_dot_ai_utilities.auth.auth_api import (
    AuthApiClient,
    AuthApiConfigurationError,
    AuthApiRequestError,
    UserAuthorisationResult,
)
from i_dot_ai_utilities.auth.auth_reason import AuthReason
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger

test_app = "test_app"
test_token = "test_token"  # noqa: S105
test_url = "https://test-url.test"


@pytest.mark.parametrize(
    "is_authorised",
    [
        True,
        False,
    ],
)
@patch.object(
    requests,
    "post",
)
def test_auth_api_response_extracts_expected_fields(mock_requests_response, is_authorised, logger):
    mock_requests_response.return_value = get_mock_requests_response(authed=is_authorised)

    client = AuthApiClient(test_app, test_url, logger)

    response = client.get_user_authorisation_info(test_token)

    called_args, called_kwargs = mock_requests_response.call_args
    payload = called_kwargs.get("json")

    assert called_args[0] == test_url + "/tokens/authorise"

    assert isinstance(payload, dict)
    assert payload["app_name"] == test_app
    assert payload["token"] == test_token

    assert response.email == "mocked@test.com"
    assert response.is_authorised == is_authorised
    assert response.auth_reason == AuthReason.JWT_GLOBAL_ACCESS_CLAIM


@patch.object(
    requests,
    "post",
)
def test_auth_api_coerces_unknown_auth_reason_to_unknown(mock_requests_response, logger):
    mock_response = get_mock_requests_response(authed=False)
    mock_response.json.return_value["decision"]["auth_reason"] = "FUTURE_REASON_NOT_IN_ENUM"
    mock_requests_response.return_value = mock_response

    client = AuthApiClient(test_app, test_url, logger)

    response = client.get_user_authorisation_info(test_token)

    assert response.auth_reason == AuthReason.UNKNOWN


@patch.object(
    requests,
    "post",
)
def test_auth_api_handles_non_ok_response_as_expected(mock_requests_response, logger):
    mock_requests_response.return_value = get_mock_requests_response(authed=True, is_errored=True)

    client = AuthApiClient(test_app, test_url, logger)

    with pytest.raises(AuthApiRequestError):
        client.get_user_authorisation_info(test_token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_authorised",
    [
        True,
        False,
    ],
)
async def test_async_auth_api_response_extracts_expected_fields(is_authorised: bool, logger: StructuredLogger) -> None:
    async_client = get_mock_async_client(get_mock_httpx_response(authed=is_authorised))

    client = AuthApiClient(test_app, test_url, logger, async_client=async_client)

    response: UserAuthorisationResult = await client.aget_user_authorisation_info(test_token)

    called_args, called_kwargs = async_client.post.call_args
    payload = called_kwargs.get("json")

    assert called_args[0] == test_url + "/tokens/authorise"

    assert isinstance(payload, dict)
    assert payload["app_name"] == test_app
    assert payload["token"] == test_token

    assert response.email == "mocked@test.com"
    assert response.is_authorised == is_authorised
    assert response.auth_reason == AuthReason.JWT_GLOBAL_ACCESS_CLAIM


@pytest.mark.asyncio
async def test_async_auth_api_reuses_the_given_client(logger: StructuredLogger) -> None:
    async_client = get_mock_async_client(get_mock_httpx_response(authed=True))

    client = AuthApiClient(test_app, test_url, logger, async_client=async_client)

    await client.aget_user_authorisation_info(test_token)
    await client.aget_user_authorisation_info(test_token)

    assert async_client.post.call_count == 2
    assert async_client.aclose.call_count == 0


@pytest.mark.asyncio
async def test_async_auth_api_coerces_unknown_auth_reason_to_unknown(logger: StructuredLogger) -> None:
    mock_response = get_mock_httpx_response(authed=False)
    mock_response.json.return_value["decision"]["auth_reason"] = "FUTURE_REASON_NOT_IN_ENUM"

    client = AuthApiClient(test_app, test_url, logger, async_client=get_mock_async_client(mock_response))

    response: UserAuthorisationResult = await client.aget_user_authorisation_info(test_token)

    assert response.auth_reason == AuthReason.UNKNOWN


@pytest.mark.asyncio
async def test_async_auth_api_handles_non_ok_response_as_expected(logger: StructuredLogger) -> None:
    async_client = get_mock_async_client(get_mock_httpx_response(authed=True, is_errored=True))

    client = AuthApiClient(test_app, test_url, logger, async_client=async_client)

    with pytest.raises(AuthApiRequestError):
        await client.aget_user_authorisation_info(test_token)


@pytest.mark.asyncio
async def test_async_auth_api_without_a_client_raises(logger: StructuredLogger) -> None:
    client = AuthApiClient(test_app, test_url, logger)

    with pytest.raises(AuthApiConfigurationError):
        await client.aget_user_authorisation_info(test_token)
