# mypy: disable-error-code="no-untyped-def"

import json
import os

import pytest

from i_dot_ai_utilities.logging.structured_logger import (
    REQUEST_SCOPE_OWNER_MIDDLEWARE,
    StructuredLogger,
    _claim_request_scope,
    _release_request_scope,
)
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat

# Optional Django imports for the scope-ownership-with-enricher test. These
# are pytest-skipped when Django is unavailable, matching the middleware
# test-suite's pattern.
django = pytest.importorskip("django")

from django.conf import settings as django_settings  # type: ignore[import-untyped]  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        MIDDLEWARE=[],
        ROOT_URLCONF=__name__,
        SECRET_KEY="test-secret-not-for-production",
        USE_TZ=True,
    )
    django.setup()

from django.test import RequestFactory  # type: ignore[import-untyped]  # noqa: E402

from i_dot_ai_utilities.logging.types.enrichment_types import (  # noqa: E402
    ContextEnrichmentType,
)

urlpatterns: list = []


def test_simple_logger(capsys):
    logger = StructuredLogger()
    logger.info("test message")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[1].get("message") == "test message"


def test_base_context_set_correctly(capsys):
    test_app_name = "logging-unit-tests"
    test_env_name = "TEST"
    os.environ["APP_NAME"] = test_app_name
    os.environ["ENVIRONMENT"] = test_env_name

    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    first_message = "Message 1 created successfully"
    second_message = "Message 2 created successfully"

    logger.info(first_message)
    logger.info(second_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    base_context_env = parsed[0].get("env")

    assert base_context_env.get("app_name") == test_app_name
    assert base_context_env.get("environment_name") == test_env_name
    assert base_context_env.get("repo_name") == "unknown"

    assert parsed[0].get("ship_logs") == 1
    assert parsed[0].get("context_id") == parsed[1].get("context_id")


@pytest.mark.parametrize(
    "execution_environment_value",
    [
        "a_false_value",
        ExecutionEnvironmentType.LOCAL,
    ],
)
def test_execution_environments_handled(execution_environment_value, capsys):
    logger = StructuredLogger(level="info", options={"execution_environment": execution_environment_value})

    logger.info("test")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("message") == "test"


@pytest.mark.parametrize("log_format_value", ["a_false_value", LogOutputFormat.TEXT])
def test_log_format_handled_and_uses_console_logger(log_format_value, capsys):
    logger = StructuredLogger(
        level="info",
        options={
            "execution_environment": ExecutionEnvironmentType.LOCAL,
            "log_format": log_format_value,
        },
    )

    test_message = "a test message"
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
    ("ship_logs_value", "ship_logs_expected"),
    [(True, 1), (1, 1), (False, 0), (0, 0), ("a_truthy_value", 1)],
)
def test_shipped_logging_setting_handled(ship_logs_value, ship_logs_expected, capsys):
    logger = StructuredLogger(
        level="info",
        options={
            "execution_environment": ExecutionEnvironmentType.LOCAL,
            "ship_logs": ship_logs_value,
        },
    )

    test_message = "a test message"
    logger.info(test_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("ship_logs") == ship_logs_expected


def test_incorrect_log_level_handled(capsys):
    logger = StructuredLogger(
        level="a_false_value",
        options={
            "execution_environment": ExecutionEnvironmentType.LOCAL,
        },
    )

    test_message = "a test message"
    logger.info(test_message)

    logger.debug("This shouldnt be logged")

    second_test_message = "a second message"
    logger.info(second_test_message)

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert "defaulting to INFO" in parsed[0].get("message")

    assert parsed[1].get("message") == test_message
    assert parsed[2].get("message") == second_test_message


def test_log_name_set(capsys):
    logger = StructuredLogger(
        options={
            "execution_environment": ExecutionEnvironmentType.LOCAL,
            "logger_name": __name__,
        },
    )

    logger.info("test message")
    logger.refresh_context()
    logger.info("with refreshed context")

    captured = capsys.readouterr()
    log_lines = captured.out.strip().splitlines()

    parsed = []
    for line in log_lines:
        parsed.append(json.loads(line))

    assert parsed[0].get("logger_name") == __name__
    assert parsed[1].get("logger_name") == __name__


# ---------------------------------------------------------------------------
# Request-scope ownership (security finding: residual flaw)
# ---------------------------------------------------------------------------


class TestRefreshContextScopeOwnership:
    """Manual ``refresh_context()`` calls inside an active request scope are
    no-ops with a warning, preventing silent de-correlation of audit trails.
    """

    def _parse(self, capsys):
        captured = capsys.readouterr()
        return [json.loads(line) for line in captured.out.strip().splitlines()]

    def test_manual_refresh_inside_scope_is_no_op_and_warns(self, capsys):
        logger = StructuredLogger(options={"execution_environment": ExecutionEnvironmentType.LOCAL})
        logger.set_context_field("bound_by_middleware", "value-that-must-survive")

        token = _claim_request_scope(REQUEST_SCOPE_OWNER_MIDDLEWARE)
        try:
            # Scope-unaware caller (e.g. an old view) tries to refresh.
            logger.refresh_context()
            logger.info("after manual refresh attempt")
        finally:
            _release_request_scope(token)

        parsed = self._parse(capsys)
        # A warning is logged informing the operator of the skipped clear.
        warning_messages = [p.get("message", "") for p in parsed]
        assert any("refresh_context" in m and "middleware-owned" in m for m in warning_messages), (
            f"expected warning about skipped clear, got {warning_messages}"
        )

        # The post-refresh info log STILL carries the field bound before the
        # scope-aware refresh attempt — i.e. the clear was skipped.
        info_line = parsed[-1]
        assert info_line.get("bound_by_middleware") == "value-that-must-survive"

    def test_manual_refresh_outside_scope_clears_as_before(self, capsys):
        logger = StructuredLogger(options={"execution_environment": ExecutionEnvironmentType.LOCAL})
        logger.set_context_field("temporary_field", "before-refresh")
        logger.refresh_context()
        logger.info("after refresh")

        parsed = self._parse(capsys)
        # No scope was claimed: the clear ran normally and the field is gone.
        assert parsed[-1].get("temporary_field") is None

    def test_job_scope_bypasses_ownership_check(self, capsys):
        """Background workers pass ``scope='job'`` and must be able to clear
        context even when a request scope is somehow still active (e.g. an
        RQ worker running on the same thread as Gunicorn in dev)."""
        logger = StructuredLogger(options={"execution_environment": ExecutionEnvironmentType.LOCAL})
        logger.set_context_field("stale_request_field", "leftover")

        token = _claim_request_scope(REQUEST_SCOPE_OWNER_MIDDLEWARE)
        try:
            logger.refresh_context(scope="job")
            logger.info("after job refresh")
        finally:
            _release_request_scope(token)

        parsed = self._parse(capsys)
        # Job scope opts out of the ownership check — field is cleared.
        assert parsed[-1].get("stale_request_field") is None

    def test_enrichers_still_apply_when_clear_is_skipped(self, capsys):
        """When the clear is skipped, enrichers passed to ``refresh_context``
        should still be applied on top of the existing context so callers
        that wanted to add fields aren't silently dropped.

        Uses a real ``HttpRequest`` so the strict isinstance check in the
        Django enricher (security finding A1) accepts it.
        """
        request = RequestFactory().get("/x")

        logger = StructuredLogger(options={"execution_environment": ExecutionEnvironmentType.LOCAL})
        logger.set_context_field("keep_me", "from-middleware")

        token = _claim_request_scope(REQUEST_SCOPE_OWNER_MIDDLEWARE)
        try:
            logger.refresh_context(
                context_enrichers=[
                    {
                        "type": ContextEnrichmentType.DJANGO,
                        "object": request,
                    }
                ],
            )
            logger.info("after skipped-clear-with-enrichers")
        finally:
            _release_request_scope(token)

        parsed = self._parse(capsys)
        line = parsed[-1]
        # Pre-existing field survived the skipped clear.
        assert line.get("keep_me") == "from-middleware"
        # Enricher-sourced field was still applied.
        assert line.get("http.request.method") == "GET"
