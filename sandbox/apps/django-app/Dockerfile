FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Django, gunicorn and OTel deps at build-time so the per-start
# `pip install -e /opt/utility[django,otel]` is a no-op apart from the
# local package itself.
RUN pip install \
    "django>=4.2,<6.0" \
    "gunicorn>=22.0" \
    "opentelemetry-api>=1.27" \
    "opentelemetry-sdk>=1.27" \
    "opentelemetry-exporter-otlp-proto-http>=1.27" \
    "opentelemetry-instrumentation-django>=0.48b0" \
    "opentelemetry-propagator-aws-xray>=1.0.1"

COPY manage.py /app/manage.py
COPY demo /app/demo

# Install the local i-dot-ai-utilities (bind-mounted at /opt/utility) with
# BOTH [django] and [otel] extras so the middleware and configure_otel_for_django
# are available.
CMD ["sh", "-c", "pip install -e '/opt/utility[django,otel]' && exec gunicorn demo.wsgi:application --bind 0.0.0.0:8003 --workers 2 --access-logfile -"]
