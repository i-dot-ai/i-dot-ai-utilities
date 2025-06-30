# mypy: disable-error-code="no-untyped-def"

import json

import pytest
from i_dot_ai_utilities.metrics.cloudwatch_emf import CloudwatchEmbeddedMetricsWriter
from i_dot_ai_utilities.metrics.interfaces import MetricsWriter


@pytest.fixture
def metrics_writer() -> MetricsWriter:
    return CloudwatchEmbeddedMetricsWriter("test_namespace")


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
    assert cloudwatch_metrics_block[0].get("Namespace") == "test_namespace"
    assert cloudwatch_metrics_block[0].get("Dimensions", None) == []

    metric_block = cloudwatch_metrics_block[0].get("Metrics")
    assert len(metric_block) == 1
    assert metric_block[0].get('Name') == metric_name
    assert metric_block[0].get('Unit') == "Count"
    assert metric_block[0].get('StorageResolution') == 60


def test_metric_with_dimensions(capsys, metrics_writer):
    metric_name = "test_metric_with_dimensions"
    metric_value = 3

    metrics_writer.put_metric(
        metric_name=metric_name,
        value=metric_value,
        dimensions={
            "dim1": "res1",
            "dim2": "res2",
        }
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
        logged_metric
        .get("_aws")
        .get("CloudWatchMetrics")[0]
        .get("Dimensions")[0]
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
        logged_metric
        .get("_aws")
        .get("CloudWatchMetrics")[0]
        .get("Metrics")[0]
    )

    assert metric_block.get('Unit') == metric_unit
