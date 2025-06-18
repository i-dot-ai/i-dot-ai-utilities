import os
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
import json
import pytest


def test_simple_logger(capsys):
    logger = StructuredLogger()
    logger.info('test message')

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))
    
    assert parsed[1].get('message') == "test message"


def test_base_context_set_correctly(capsys):
    test_app_name = "logging-unit-tests"
    test_env_name = "TEST"
    os.environ["APP_NAME"] = test_app_name
    os.environ["ENVIRONMENT"] = test_env_name

    logger = StructuredLogger(level='info', options={
        "execution_environment": ExecutionEnvironmentType.LOCAL
    })

    first_message = "Message 1 created successfully"
    second_message = "Message 2 created successfully"

    logger.info(first_message)
    logger.info(second_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get('env_app_name') == test_app_name
    assert parsed[0].get('ship_logs') == 1
    assert parsed[0].get('env_environment_name') == test_env_name
    assert parsed[0].get('context_id') == parsed[1].get('context_id')


@pytest.mark.parametrize(
    "execution_environment_value", [
        "a_false_value",
        ExecutionEnvironmentType.LOCAL,
    ]
)
def test_execution_environments_handled(execution_environment_value, capsys):
    logger = StructuredLogger(level='info', options={
        "execution_environment": execution_environment_value
    })

    logger.info('test')

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get('message') == "test"


@pytest.mark.parametrize(
    "log_format_value", [
        "a_false_value",
        LogOutputFormat.TEXT
    ]
)
def test_log_format_handled_and_uses_console_logger(log_format_value, capsys):
    logger = StructuredLogger(level='info', options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
        "log_format": log_format_value
    })

    test_message = 'a test message'
    logger.info(test_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(line)

    shipping_error_message = "messages cannot be shipped downstream outside of JSON format. Disabling log shipping"
    assert shipping_error_message in parsed[0]
    assert test_message in parsed[1]


@pytest.mark.parametrize(
    "ship_logs_value, ship_logs_expected", [
        (True, 1),
        (1, 1),
        (False, 0),
        (0, 0),
        ("a_truthy_value", 1)
    ]
)
def test_shipped_logging_setting_handled(ship_logs_value, ship_logs_expected, capsys):
    logger = StructuredLogger(level='info', options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
        "ship_logs": ship_logs_value
    })

    test_message = 'a test message'
    logger.info(test_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get('ship_logs') == ship_logs_expected


def test_incorrect_log_level_handled(capsys):
    logger = StructuredLogger(level='a_false_value', options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
    })

    test_message = 'a test message'
    logger.info(test_message)

    logger.debug('This shouldnt be logged')

    second_test_message = 'a second message'
    logger.info(second_test_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert "defaulting to INFO" in parsed[0].get('message')

    assert parsed[1].get('message') == test_message
    assert parsed[2].get('message') == second_test_message