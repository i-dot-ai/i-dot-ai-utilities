# mypy: disable-error-code="no-untyped-def"

from unittest.mock import patch

import pytest
import requests

from i_dot_ai_utilities.auth.__tests__.conftest import get_mock_requests_response
from i_dot_ai_utilities.auth.auth_api import AuthApiClient, AuthApiRequestError
from i_dot_ai_utilities.auth.auth_reason import AuthReason

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
