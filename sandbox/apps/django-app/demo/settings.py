"""Minimal Django settings for the sandbox demo app.

HTTP request attributes (method, path, route, status code, etc.) live on
OTel spans rather than the log record. Trace correlation on log records
comes from the `otel_trace_context_processor` structlog processor —
installed from `demo.apps.DemoConfig.ready()`.

``DjangoUserIdMiddleware`` is wired after the OTel middleware so that
authenticated-user attribution lands on the active request scope.
"""

from __future__ import annotations

import os
from pathlib import Path

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "sandbox-not-a-secret"  # noqa: S105 - local demo only
DEBUG = False
ALLOWED_HOSTS = ["*"]

os.environ.setdefault("APP_NAME", "django-demo")
os.environ.setdefault("REPO", "i-dot-ai-utilities-sandbox")
os.environ.setdefault("ENVIRONMENT", "sandbox")

# ---------------------------------------------------------------------------
# i-dot-ai-utilities structured logger.
# ---------------------------------------------------------------------------
LOGGER = StructuredLogger(
    level="INFO",
    options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
        "log_format": LogOutputFormat.JSON,
        "logger_name": "django-demo",
        "ship_logs": False,
    },
)

# As of i-dot-ai-utilities 0.6.0, dotted-import strings are rejected by the
# middleware's settings loader. Pass the fully-constructed logger object.
I_DOT_AI_LOGGER = LOGGER

I_DOT_AI_LOGGING_HEADER_ALLOWLIST = ("X-Tenant-ID", "X-Request-ID")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # Our demo app is an AppConfig so that ready() runs OTel setup.
    "demo.apps.DemoConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # DjangoInstrumentor (installed by configure_otel_for_django) prepends
    # its own server-span middleware at the front of the chain; we do NOT
    # list it here.
    "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
    # Bind user.id onto the log context after auth runs. In this demo
    # Django's AuthenticationMiddleware isn't wired (no sessions, no DB),
    # so request.user is absent and the middleware is a no-op; the
    # ordering is documented here so production consumers copy it.
    "i_dot_ai_utilities.logging.middleware.django_user_id.DjangoUserIdMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "demo.urls"
WSGI_APPLICATION = "demo.wsgi.application"

DATABASES: dict = {}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "root": {"handlers": ["null"], "level": "WARNING"},
}
