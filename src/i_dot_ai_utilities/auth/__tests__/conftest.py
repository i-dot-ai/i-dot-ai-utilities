# mypy: disable-error-code="no-untyped-def"

from unittest.mock import Mock

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
