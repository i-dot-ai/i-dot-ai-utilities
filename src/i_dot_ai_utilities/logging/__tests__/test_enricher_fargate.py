from unittest.mock import patch
import i_dot_ai_utilities
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
import json
import pytest

@pytest.fixture
def load_test_metadata_object():
    return {
        "ImageID": "image12345",
        "StartedAt": "2023-07-21T15:45:44.954460255Z",
        "Labels": {
             "com.amazonaws.ecs.task-arn": "arn:aws:ecs:us-east-1:123456789012:task/testcluster/testarn",
        },
    }


@patch.object(i_dot_ai_utilities.logging.enrichers.fargate_enricher.FargateEnvironmentEnricher, '_get_metadata_response')
def test_fargate_enriched_logger_contains_expected_fields(mocked_metadata_response, load_test_metadata_object, capsys):
    mocked_metadata_response.return_value = load_test_metadata_object

    logger = StructuredLogger(level='info', options={
        "execution_environment": ExecutionEnvironmentType.FARGATE
    })

    logger.info('test message')

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get('fargate_image_id') == load_test_metadata_object['ImageID']
    assert parsed[0].get('fargate_task_arn') == load_test_metadata_object['Labels']['com.amazonaws.ecs.task-arn']
    assert parsed[0].get('fargate_container_started_at') == load_test_metadata_object['StartedAt']

@pytest.mark.parametrize(
    "metadata_response_value", [
        {"a_dummy_response": True},
        None,
        0,
        "blah",
    ]
)
@patch.object(i_dot_ai_utilities.logging.enrichers.fargate_enricher.FargateEnvironmentEnricher, '_get_metadata_response')
def test_fargate_enrichment_handles_malformed_response_object(mocked_metadata_response_object, metadata_response_value, capsys):
    mocked_metadata_response_object.return_value = metadata_response_value

    StructuredLogger(level='info', options={
        "execution_environment": ExecutionEnvironmentType.FARGATE
    })

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert "Failed to extract Fargate container metadata fields" in parsed[0].get('message')
    assert "Response doesn't conform to FargateContainerMetadataResponse" in parsed[0].get('exception')

def test_logger_handles_exception_if_outside_of_fargate_environment(capsys):
    logger = StructuredLogger(level='info', options={
        "execution_environment": ExecutionEnvironmentType.FARGATE
    })

    second_message = "Second message created successfully"
    logger.info(second_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert "Failed to find metadata URL on environment" in parsed[0].get('exception')
    assert (parsed[0]).get('message') == "Exception(Logger): Failed to extract Fargate container metadata fields"
    assert (parsed[0]).get('level') == "error"
    
    assert (parsed[1]).get('message') == second_message
    assert (parsed[1]).get('level') == "info"
