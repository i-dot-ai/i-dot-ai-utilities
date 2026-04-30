FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The i-dot-ai-utilities source tree is bind-mounted read-only at /opt/utility
# at runtime. We install it in editable mode so edits to the library are
# picked up by simply restarting the container (no rebuild).
COPY pyproject.toml /app/pyproject.toml
RUN pip install fastapi "uvicorn[standard]"

COPY main.py /app/main.py

# install the local utility in editable mode at container start so the
# bind-mounted source is present (it isn't at build time).
CMD ["sh", "-c", "pip install -e /opt/utility && exec uvicorn main:app --host 0.0.0.0 --port 8001 --log-level warning"]
