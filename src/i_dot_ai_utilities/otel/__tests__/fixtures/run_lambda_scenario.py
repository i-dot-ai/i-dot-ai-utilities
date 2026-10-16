"""Subprocess runner for isolated Lambda and local OTel scenarios."""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sys
import time
import traceback
from typing import Any


class _FakeLambdaContext:
    def __init__(
        self,
        *,
        function_name: str = "core-auth-api-authorise-token",
        aws_request_id: str = "req-1234",
        remaining_ms: int = 4500,
    ) -> None:
        self.function_name = function_name
        self.function_version = "$LATEST"
        self.invoked_function_arn = f"arn:aws:lambda:eu-west-2:123456789012:function:{function_name}"
        self.memory_limit_in_mb = 512
        self.aws_request_id = aws_request_id
        self.log_group_name = f"/aws/lambda/{function_name}"
        self.log_stream_name = "2026/10/16/[$LATEST]abcdef"
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def _install_simulated_lambda_runtime_handlers() -> tuple[io.StringIO, io.StringIO]:
    awslambdaric_mod = type(sys)("awslambdaric.bootstrap")
    sys.modules["awslambdaric.bootstrap"] = awslambdaric_mod

    class LambdaLoggerHandler(logging.StreamHandler):  # type: ignore[type-arg]
        pass

    class LambdaLoggerHandlerWithFrameType(logging.StreamHandler):  # type: ignore[type-arg]
        pass

    LambdaLoggerHandler.__module__ = "awslambdaric.bootstrap"
    LambdaLoggerHandlerWithFrameType.__module__ = "awslambdaric.bootstrap"

    runtime_buf = io.StringIO()
    user_buf = io.StringIO()
    root_logger = logging.getLogger()
    root_logger.addHandler(LambdaLoggerHandler(runtime_buf))
    root_logger.addHandler(LambdaLoggerHandlerWithFrameType(runtime_buf))
    if os.environ.get("TEST_INSTALL_USER_HANDLER") == "1":
        root_logger.addHandler(logging.StreamHandler(user_buf))
    return runtime_buf, user_buf


def main() -> int:
    scenario = json.loads(sys.argv[1])
    runtime_buf, user_buf = _install_simulated_lambda_runtime_handlers()

    mod = importlib.import_module("i_dot_ai_utilities.otel.__tests__.fixtures.sample_lambda_handler")
    results: list[dict[str, Any]] = []

    for call in scenario.get("invocations", []):
        ctx = _FakeLambdaContext(
            aws_request_id=call.get("request_id", "req-1234"),
            remaining_ms=int(call.get("remaining_ms", 4500)),
        )
        start = time.monotonic()
        try:
            res = mod.lambda_handler(call.get("event", {}), ctx)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.monotonic() - start) * 1000.0
            results.append(
                {
                    "raised": type(exc).__name__,
                    "message": str(exc),
                    "traceback": "".join(traceback.format_exception(exc)),
                    "elapsed_ms": elapsed_ms,
                }
            )
        else:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            results.append({"returned": res, "elapsed_ms": elapsed_ms})

    summary = {
        "results": results,
        "runtime_handler_output": runtime_buf.getvalue(),
        "user_handler_output": user_buf.getvalue(),
        "structlog_imported": "structlog" in sys.modules,
    }
    sys.stderr.write("SCENARIO_JSON:" + json.dumps(summary) + "\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
