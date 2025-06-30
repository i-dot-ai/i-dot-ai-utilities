# mypy: disable-error-code="no-untyped-def"

import json

import pytest

from i_dot_ai_utilities.metrics.cloudwatch_emf import CloudwatchEmbeddedMetricsWriter
from i_dot_ai_utilities.metrics.interfaces import MetricsWriter


@pytest.fixture
def metrics_writer() -> MetricsWriter:
    return CloudwatchEmbeddedMetricsWriter("test_namespace", "test_environment")


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
    assert (
        cloudwatch_metrics_block[0].get("Namespace")
        == "test_namespace/test_environment"
    )
    assert cloudwatch_metrics_block[0].get("Dimensions", None) == []

    metric_block = cloudwatch_metrics_block[0].get("Metrics")
    assert len(metric_block) == 1
    assert metric_block[0].get("Name") == metric_name
    assert metric_block[0].get("Unit") == "Count"
    assert metric_block[0].get("StorageResolution") == 60


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

    dimension_names = (
        logged_metric.get("_aws").get("CloudWatchMetrics")[0].get("Dimensions")[0]
    )

    assert len(dimension_names) == 2
    assert dimension_names[0] == "dim1"
    assert dimension_names[1] == "dim2"


def test_metric_with_unit_set(capsys, metrics_writer):
    metric_name = "test_metric_with_unit_set"
    metric_value = 4.5
    metric_unit = "test_unit"

    metrics_writer.put_metric(
        metric_name=metric_name,
        value=metric_value,
        unit=metric_unit,
    )

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    logged_metric = parsed[0]

    metric_block = (
        logged_metric.get("_aws").get("CloudWatchMetrics")[0].get("Metrics")[0]
    )

    assert metric_block.get("Unit") == metric_unit


def test_gracefully_handles_badly_set_dimension(capsys, metrics_writer):
    metric_name = "test_gracefully_handles_badly_set_dimension"

    metrics_writer.put_metric(
        metric_name=metric_name, value=1, dimensions={"this fails"}
    )
    metrics_writer.put_metric(
        metric_name=metric_name, value=1, dimensions={"this": "succeeds"}
    )

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    assert ("Failed to write metric") in log_lines[0]
    assert json.loads(log_lines[1]).get("this") == "succeeds"


@pytest.mark.parametrize(
    ("metric_name", "metric_value", "metric_unit", "expected_error_message"),
    [
        (None, 1, "Count", "Missing required parameter"),
        ("test_metric", None, "Count", "Missing required parameter"),
        (-99, 1, "Count", "Incorrect parameter type"),
        ("test_metric", "broken_value", "Count", "Incorrect parameter type"),
        ("test_metric", 1, -99, "Incorrect parameter type"),
    ],
)
def test_gracefully_handles_badly_set_field(
    capsys,
    metrics_writer,
    metric_name,
    metric_value,
    metric_unit,
    expected_error_message,
):
    metrics_writer.put_metric(
        metric_name=metric_name,
        value=metric_value,
        unit=metric_unit,
    )
    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    assert ("Failed to write metric") in log_lines[0]
    assert (expected_error_message) in log_lines[0]
