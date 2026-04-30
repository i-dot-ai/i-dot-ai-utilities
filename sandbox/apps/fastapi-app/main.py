"""FastAPI demo app that exercises every code path of the i-dot-ai-utilities StructuredLogger.

All endpoints refresh the logger context with the FastAPI enricher at the top of the
handler so each log line carries OTel http.* / url.* attributes derived from the
request.
"""

from __future__ import annotations

import asyncio
import os
import random

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import (
    ContextEnrichmentType,
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat

os.environ.setdefault("APP_NAME", "fastapi-demo")
os.environ.setdefault("REPO", "i-dot-ai-utilities-sandbox")
os.environ.setdefault("ENVIRONMENT", "sandbox")

logger = StructuredLogger(
    level="INFO",
    options={
        "execution_environment": ExecutionEnvironmentType.LOCAL,
        "log_format": LogOutputFormat.JSON,
        "logger_name": "fastapi-demo",
        # keep ship_logs off here; JSON is enough for filelog to parse.
        "ship_logs": False,
    },
)

app = FastAPI(title="fastapi-demo")


def _refresh(request: Request) -> None:
    """Helper: refresh context with the FastAPI enricher."""
    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.FASTAPI,
                "object": request,
            }
        ]
    )


@app.get("/")
async def index(request: Request) -> dict[str, str]:
    _refresh(request)
    logger.info("index hit")
    return {"app": "fastapi-demo", "message": "hello"}


@app.get("/users/{user_id}")
async def get_user(user_id: int, request: Request) -> dict[str, int | str]:
    _refresh(request)
    logger.set_context_field("user_id", user_id)
    logger.info("fetching user {id}", id=user_id)
    if user_id < 0:
        logger.warning("negative user id {id}", id=user_id)
        raise HTTPException(status_code=400, detail="negative id")
    return {"user_id": user_id, "name": f"user-{user_id}"}


@app.get("/search")
async def search(request: Request, q: str = "") -> dict[str, str]:
    _refresh(request)
    logger.info("search performed with {query}", query=q)
    return {"query": q}


@app.get("/slow")
async def slow(request: Request) -> dict[str, str]:
    _refresh(request)
    logger.warning("slow endpoint entered")
    await asyncio.sleep(0.3)
    logger.info("slow endpoint complete")
    return {"status": "slow-ok"}


@app.get("/boom")
async def boom(request: Request) -> JSONResponse:
    _refresh(request)
    try:
        denom = random.choice([0, 0, 0])  # noqa: S311 - demo only
        return JSONResponse({"result": 1 / denom})
    except ZeroDivisionError:
        logger.exception("boom endpoint exploded")
        return JSONResponse({"error": "zero division"}, status_code=500)


@app.get("/health")
async def health() -> dict[str, str]:
    # deliberately no refresh + no log - mirrors real low-noise health probes.
    return {"status": "ok"}
