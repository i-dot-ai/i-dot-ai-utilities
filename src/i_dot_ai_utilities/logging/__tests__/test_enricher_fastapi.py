# mypy: disable-error-code="no-untyped-def"

import json

import pytest
from starlette.requests import Request

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import (
    ContextEnrichmentType,
    ExecutionEnvironmentType,
)


@pytest.fixture
def load_test_request_object() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/logger/unit/testing",
        "headers": [(b"user-agent", b"test agent")],
        "query_string": b"islogger=true&istest=true",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
        "asgi": {"spec_version": "2.1", "version": "3.0"},
    }
    return Request(scope)


def test_fastapi_enriched_logger_contains_expected_fields(
    load_test_request_object, capsys
):
    logger = StructuredLogger(
        level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL}
    )

    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.FASTAPI,
                "object": load_test_request_object,
            }
        ]
    )

    logger.info("test message")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    req_object = (parsed[0]).get("request")

    assert (req_object).get("method") == "GET"
    assert (req_object).get("base_url") == "http://testserver/"
    assert (req_object).get("path") == "/logger/unit/testing"
    assert (req_object).get("user_agent") == "test agent"
    assert (req_object).get("query") == "islogger=true&istest=true"


@pytest.mark.parametrize(
    "fastpi_request_object_value",
    [
        {"a_dummy_response": True},
        None,
        0,
        "blah",
    ],
)
def test_fastapi_enrichment_handles_malformed_object(
    fastpi_request_object_value, capsys
):
    logger = StructuredLogger(
        level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL}
    )

    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.FASTAPI,
                "object": fastpi_request_object_value,
            }
        ]
    )

    log_message = "logger continues working as normal"
    logger.warning(log_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert "doesn't conform to RequestLike. Context not set" in parsed[0].get(
        "exception"
    )
    assert parsed[0].get("level") == "error"

    assert parsed[1].get("message") == log_message
    assert parsed[1].get("level") == "warning"
