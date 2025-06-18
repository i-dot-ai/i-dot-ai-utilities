import json
import logging

import pytest

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType


def test_all_log_levels_log_as_expected(capsys):
    logger = StructuredLogger("debug", options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
    })

    message = "test message"
    logger.debug(message)
    logger.info(message)
    logger.warning(message)
    logger.error(message)

    try:
        1 / 0
    except:
        logger.exception(message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("level") == "debug"
    assert parsed[1].get("level") == "info"
    assert parsed[2].get("level") == "warning"
    assert parsed[3].get("level") == "error"

    assert parsed[4].get("level") == "error"
    assert "ZeroDivisionError" in parsed[4].get("exception")


@pytest.mark.parametrize(
    "log_level, expected_log_count", [
        ("debug", 4),
        ("info", 3),
        ("warning", 2),
        ("error", 1),
    ]
)
def test_log_levels_omit_logs_if_below_set_level(log_level, expected_log_count, capsys):
    logger = StructuredLogger(log_level, options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
    })

    message = "test message"

    logger.debug(message)
    logger.info(message)
    logger.warning(message)
    logger.error(message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert len(parsed) == expected_log_count


def test_log_message_interpolation_works_and_fields_added(capsys):
    logger = StructuredLogger(logging.INFO, options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
    })

    templated_message_string = "This is a test message. Email: {email}, ID: {id}. Fields will be interpolated"
    email="foo@baz.com"
    id=12345

    logger.info(
        templated_message_string,
        email=email,
        id=id,
    )

    test_dict = {"foo": {"bar" : "baz"}}
    templated_message_dict = "This is a test with a nested dictionary. Dictionary is {test_dict}. Fin"
    logger.info(
        templated_message_dict,
        test_dict=test_dict
    )

    test_array = [0, 1, 2, 3, [4, "test_item"], 5]
    templated_message_array = "This is a test with a nested array. Array is {test_array}. Fin"
    logger.info(
        templated_message_array,
        test_array=test_array
    )

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("message") == "This is a test message. Email: foo@baz.com, ID: 12345. Fields will be interpolated"
    assert parsed[0].get("message_template") == templated_message_string
    assert parsed[0].get("email") == email
    assert parsed[0].get("id") == id

    assert isinstance(parsed[1].get("message"), str)
    assert parsed[1].get("test_dict").get("foo").get("bar") == "baz"

    assert isinstance(parsed[2].get("message"), str)
    assert parsed[2].get("test_array")[4][1] == "test_item"


def test_string_interpolation_failure_handled_by_logger(capsys):
    logger = StructuredLogger(logging.INFO, options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
    })

    templated_message_string = "This is a test message. Email: {missing}"

    logger.info(templated_message_string)

    logger.info("should log successfully")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("message") == "Exception(Logger): Variable interpolation failed when formatting log message. Is a value missing?"

    assert parsed[1].get("message") == templated_message_string
    assert parsed[1].get("message_template") == templated_message_string

    assert parsed[2].get("message") == "should log successfully"


def test_context_refresh_resets_context(capsys):
    logger = StructuredLogger(logging.INFO, options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
    })

    logger.info("Initial test message", added_context="initial_context")

    logger.refresh_context()

    logger.info("Another test message without context")
    logger.info("Yet another, with context", added_context="more_context")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("added_context") == "initial_context"
    assert parsed[1].get("added_context", "no such key") == "no such key"
    assert parsed[2].get("added_context") == "more_context"

    assert parsed[0].get("context_id") != parsed[1].get("context_id")
    assert parsed[1].get("context_id") == parsed[2].get("context_id")
