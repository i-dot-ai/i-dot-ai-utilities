"""Subprocess runner: a FastAPI app on the ECS runtime, driven by Starlette's TestClient.

``configure_otel()`` runs before FastAPI is imported, as the README requires,
so the FastAPI instrumentor can patch ``fastapi.FastAPI``. The process then
exits normally, so the capped ``atexit`` shutdown is part of what is timed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from i_dot_ai_utilities.otel import configure_otel, iai_attribute

configure_otel(service_name=os.environ.get("TEST_EXPLICIT_SERVICE_NAME") or None)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

logger = logging.getLogger("sample.api")
app = FastAPI()


@app.get("/items/{item_id}")
def read_item(item_id: int) -> dict[str, int]:
    logger.info("Item requested", extra={iai_attribute("sample.item.id"): item_id})
    return {"item_id": item_id}


def main() -> int:
    requests = int(os.environ.get("TEST_REQUEST_COUNT", "3"))
    latencies_ms: list[float] = []
    statuses: list[int] = []
    with TestClient(app) as client:
        for idx in range(requests):
            start = time.monotonic()
            response = client.get(f"/items/{idx}")
            latencies_ms.append((time.monotonic() - start) * 1000.0)
            statuses.append(response.status_code)
    if os.environ.get("TEST_WAIT_FOR_EXPORT_SECONDS"):
        # Give the background export thread time to attempt (and fail) an export.
        time.sleep(float(os.environ["TEST_WAIT_FOR_EXPORT_SECONDS"]))
    sys.stderr.write("SCENARIO_JSON:" + json.dumps({"latencies_ms": latencies_ms, "statuses": statuses}) + "\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
