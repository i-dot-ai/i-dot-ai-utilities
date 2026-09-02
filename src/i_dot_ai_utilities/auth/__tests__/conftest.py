# mypy: disable-error-code="no-untyped-def"

from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import requests

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


@pytest.fixture
def logger():
    return StructuredLogger()


def get_mock_requests_response(authed: bool, is_errored=False):
    payload = {
        "metadata": {"user_email": "mocked@test.com", "signing_party": "keycloak"},
        "decision": {"is_authorised": authed, "auth_reason": "JWT_GLOBAL_ACCESS_CLAIM"},
    }

    mock_response = Mock()

    mock_response.ok = not is_errored

    mock_response.json.return_value = payload

    if is_errored:
        mock_response.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
    else:
        mock_response.raise_for_status = Mock()

    return mock_response


def get_mock_httpx_response(authed: bool, is_errored: bool = False) -> Mock:
    """Mock async response."""

    payload: dict[str, Any] = {
        "metadata": {"user_email": "mocked@test.com", "signing_party": "keycloak"},
        "decision": {"is_authorised": authed, "auth_reason": "JWT_GLOBAL_ACCESS_CLAIM"},
    }

    mock_response = Mock()

    mock_response.json.return_value = payload

    if is_errored:
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=Mock(),
            response=mock_response,
        )
    else:
        mock_response.raise_for_status = Mock()

    return mock_response


def get_mock_async_client(mock_response: httpx.Response) -> Mock:
    """Stand in for the caller-supplied, long-lived `httpx.AsyncClient` the async path requires."""

    mock_client = Mock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)

    return mock_client
