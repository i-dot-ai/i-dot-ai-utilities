# mypy: disable-error-code="no-untyped-def"

import json

import pytest

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.metrics.cloudwatch import CloudwatchEmbeddedMetricsWriter
from i_dot_ai_utilities.metrics.interfaces import MetricsWriter
from i_dot_ai_utilities.metrics.unit import Unit


@pytest.fixture
def metrics_writer() -> MetricsWriter:
    return CloudwatchEmbeddedMetricsWriter(
        namespace="test_namespace",
        environment="test_environment",
        logger=StructuredLogger(
            options={"execution_environment": ExecutionEnvironmentType.LOCAL},
        ),
    )


def test_simple_metric(capsys, metrics_writer):
    metric_name = "test_simple_metric"
    metric_value = 1.5

    metrics_writer.put_metric(metric_name=metric_name, value=metric_value)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    logged_metric = parsed[0]

    assert logged_metric.get(metric_name) == metric_value

    cloudwatch_metrics_block = logged_metric.get("_aws").get("CloudWatchMetrics")
    assert len(cloudwatch_metrics_block) == 1
    assert cloudwatch_metrics_block[0].get("Namespace") == "test_namespace/test_environment"
    assert cloudwatch_metrics_block[0].get("Dimensions", None) == []

    metric_block = cloudwatch_metrics_block[0].get("Metrics")
    assert len(metric_block) == 1
    assert metric_block[0].get("Name") == metric_name
    assert metric_block[0].get("Unit") == "Count"
    assert metric_block[0].get("StorageResolution") == 60


@pytest.mark.parametrize("value", [0, 0.0])
def test_zero_metric_is_emitted(capsys, metrics_writer, value):
    metrics_writer.put_metric("zero_metric", value)

    logged_metric = json.loads(capsys.readouterr().out)
    assert logged_metric["zero_metric"] == value
    assert logged_metric["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Count"
    assert "message" not in logged_metric


@pytest.mark.parametrize("unit", [*Unit, "seconds", "MILLISECONDS", "count/second", "none"])
def test_metric_with_unit(capsys, metrics_writer, unit):
    metrics_writer.put_metric("unit_metric", 0.0, {"Component": "transcription"}, unit=unit)

    logged_metric = json.loads(capsys.readouterr().out)
    directive = logged_metric["_aws"]["CloudWatchMetrics"][0]
    assert directive["Metrics"][0]["Unit"] == unit.title()
    assert directive["Dimensions"] == [["Component"]]
    assert logged_metric["Component"] == "transcription"
    assert logged_metric["unit_metric"] == 0.0


@pytest.mark.parametrize("unit", ["unsupported", "", None, 1])
def test_gracefully_handles_invalid_unit(capsys, metrics_writer, unit):
    metrics_writer.put_metric("unit_metric", 1, unit=unit)

    logged_message = json.loads(capsys.readouterr().out)
    assert logged_message["message"] == "Failed to write metric"
    assert "Invalid metric unit" in logged_message["exception"]
    assert "_aws" not in logged_message


def test_unit_can_be_passed_positionally(capsys, metrics_writer):
    metrics_writer.put_metric("count_metric", 0, None, Unit.Count)

    logged_metric = json.loads(capsys.readouterr().out)
    assert logged_metric["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Count"


def test_metric_with_dimensions(capsys, metrics_writer):
    metric_name = "test_metric_with_dimensions"
    metric_value = 3

    metrics_writer.put_metric(
        metric_name=metric_name,
        value=metric_value,
        dimensions={
            "dim1": "res1",
            "dim2": "res2",
        },
    )

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    logged_metric = parsed[0]

    assert logged_metric.get("dim1") == "res1"
    assert logged_metric.get("dim2") == "res2"

    dimension_names = logged_metric.get("_aws").get("CloudWatchMetrics")[0].get("Dimensions")[0]

    assert len(dimension_names) == 2
    assert dimension_names[0] == "dim1"
    assert dimension_names[1] == "dim2"


def test_gracefully_handles_badly_set_dimension(capsys, metrics_writer):
    metric_name = "test_gracefully_handles_badly_set_dimension"

    metrics_writer.put_metric(metric_name=metric_name, value=1, dimensions={"this fails"})
    metrics_writer.put_metric(metric_name=metric_name, value=1, dimensions={"this": "succeeds"})

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("message") == "Failed to write metric"
    assert json.loads(log_lines[1]).get("this") == "succeeds"


@pytest.mark.parametrize(
    ("metric_name", "metric_value", "expected_error_message"),
    [
        (None, 1, "Missing required parameter"),
        ("", 1, "Missing required parameter"),
        ("test_metric", None, "Missing required parameter"),
        (-99, 1, "Incorrect parameter type"),
        ("test_metric", "broken_value", "Incorrect parameter type"),
        ("test_metric", False, "Incorrect parameter type"),
        ("test_metric", True, "Incorrect parameter type"),
    ],
)
def test_gracefully_handles_badly_set_field(
    capsys,
    metrics_writer,
    metric_name,
    metric_value,
    expected_error_message,
):
    metrics_writer.put_metric(
        metric_name=metric_name,
        value=metric_value,
    )
    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("message") == "Failed to write metric"
    assert (expected_error_message) in parsed[0].get("exception")
