# mypy: disable-error-code="no-untyped-def"

import json
from types import SimpleNamespace

import pytest

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import (
    ContextEnrichmentType,
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.types.lambda_enrichment_schema import LambdaContextLike


@pytest.fixture
def load_test_request_object() -> LambdaContextLike:
    return SimpleNamespace(
        aws_request_id="abc123",
        invoked_function_arn="arn:aws:foo:bar:lambda/baz",
    )


def test_lambda_context_enriched_logger_contains_expected_fields(load_test_request_object, capsys):
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.LAMBDA,
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

    req_object = (parsed[0]).get("lambda_context")

    assert (req_object).get("request_id") == "abc123"
    assert (req_object).get("function_arn") == "arn:aws:foo:bar:lambda/baz"


@pytest.mark.parametrize(
    "lambda_context_object_value",
    [
        {"a_dummy_response": True},
        None,
        0,
        "blah",
    ],
)
def test_fastapi_enrichment_handles_malformed_object(lambda_context_object_value, capsys):
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.LAMBDA,
                "object": lambda_context_object_value,
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

    assert "doesn't conform to LambdaContextLike. Context not set" in parsed[0].get("exception")
    assert parsed[0].get("level") == "error"

    assert parsed[1].get("message") == log_message
    assert parsed[1].get("level") == "warning"
