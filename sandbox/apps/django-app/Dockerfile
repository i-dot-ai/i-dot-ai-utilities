FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install "django>=4.2,<6.0" gunicorn

COPY manage.py /app/manage.py
COPY demo /app/demo

# Install the local i-dot-ai-utilities at container start (bind-mounted at /opt/utility).
# Use the [django] extra so the middleware's optional dep chain is satisfied.
CMD ["sh", "-c", "pip install -e /opt/utility[django] && exec gunicorn demo.wsgi:application --bind 0.0.0.0:8002 --workers 2 --access-logfile -"]
