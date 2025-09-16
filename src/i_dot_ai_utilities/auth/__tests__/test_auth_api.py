# mypy: disable-error-code="no-untyped-def"

import json
from unittest.mock import Mock, patch
import pytest
import requests

from i_dot_ai_utilities.auth.__tests__.conftest import get_mock_requests_response
from i_dot_ai_utilities.auth.auth_api import AuthApiClient, AuthApiRequestError
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


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

    client = AuthApiClient("test_app", "https://test-url.test", logger)

    response = client.get_user_authorisation_info("test_token")

    print(response)

    assert response.email == "mocked@test.com"
    assert response.is_authorised == is_authorised


@patch.object(
    requests,
    "post",
)
def test_auth_api_handles_non_ok_response_as_expected(mock_requests_response, logger):
    mock_requests_response.return_value = get_mock_requests_response(authed=True, is_errored=True)

    client = AuthApiClient("test_app", "https://test-url.test", logger)

    with pytest.raises(AuthApiRequestError):
        client.get_user_authorisation_info("test_token")
