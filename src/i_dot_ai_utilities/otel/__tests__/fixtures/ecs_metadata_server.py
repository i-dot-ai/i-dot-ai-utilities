"""Fake ECS task metadata endpoint (v4) for container runtime tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from typing_extensions import Self

CONTAINER_ARN = "arn:aws:ecs:eu-west-2:123456789012:container/test-cluster/abc123/def456"
TASK_ARN = "arn:aws:ecs:eu-west-2:123456789012:task/test-cluster/abc123"

CONTAINER_METADATA: dict[str, Any] = {
    "ContainerARN": CONTAINER_ARN,
    "LogDriver": "awslogs",
    "LogOptions": {
        "awslogs-group": "/ecs/test-service",
        "awslogs-region": "eu-west-2",
        "awslogs-stream": "ecs/app/abc123",
    },
}
TASK_METADATA: dict[str, Any] = {
    "TaskARN": TASK_ARN,
    "Cluster": "test-cluster",
    "LaunchType": "FARGATE",
    "Family": "test-service",
    "Revision": "7",
}


class FakeEcsMetadataServer:
    """Serves ``<base>`` (container metadata) and ``<base>/task`` (task metadata) on loopback."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def uri(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/v4/abc123"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    @staticmethod
    def _make_handler() -> type[BaseHTTPRequestHandler]:
        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                payload = TASK_METADATA if self.path.endswith("/task") else CONTAINER_METADATA
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return _Handler
