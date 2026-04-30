"""Minimal Django settings for the sandbox demo app."""

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
# i-dot-ai-utilities structured logger, shared across the process
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

# The middleware's settings-loader no longer accepts a dotted import string
# (security hardening in 0.6.0). Pass the logger object directly.
I_DOT_AI_LOGGER = LOGGER

I_DOT_AI_LOGGING_HEADER_ALLOWLIST = ("X-Tenant-ID", "X-Request-ID")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "i_dot_ai_utilities.logging.middleware.django.StructuredLoggingMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "demo.urls"
WSGI_APPLICATION = "demo.wsgi.application"

DATABASES: dict = {}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# Silence Django's own stdlib logging to keep the stream clean of anything
# that isn't our structured JSON.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "root": {"handlers": ["null"], "level": "WARNING"},
}
