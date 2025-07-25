# mypy: disable-error-code="no-untyped-def"

import json
import os

import pytest

from i_dot_ai_utilities.logging.enrichers.lambda_environment_enricher import (
    load_lambda_environment_variables,
)
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType


@pytest.fixture(autouse=True)
def clear_lru_cache():
    load_lambda_environment_variables.cache_clear()


def set_environment_variables() -> None:
    os.environ["AWS_DEFAULT_REGION"] = "eu-test-1"
    os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "test-function"


def delete_environment_variables() -> None:
    del os.environ["AWS_DEFAULT_REGION"]
    del os.environ["AWS_LAMBDA_FUNCTION_NAME"]


def test_lambda_environment_enriched_logger_contains_expected_fields(capsys):
    set_environment_variables()

    logger = StructuredLogger(
        level="info",
        options={"execution_environment": ExecutionEnvironmentType.LAMBDA},
    )

    logger.info("test message")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert (parsed[0]).get("lambda_os").get("function_name") == "test-function"
    assert (parsed[0]).get("lambda_os").get("aws_region") == "eu-test-1"


def test_lambda_environment_enrichment_handles_missing_vars(capsys):
    delete_environment_variables()

    logger = StructuredLogger(
        level="info",
        options={"execution_environment": ExecutionEnvironmentType.LAMBDA},
    )

    success_msg = "should still be working"
    logger.info(success_msg)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert "Failed to extract Lambda environment variables" in parsed[0].get("message")
    assert "validation errors for LambdaEnvironmentSettings" in parsed[0].get("exception")

    assert parsed[1].get("message") == success_msg
