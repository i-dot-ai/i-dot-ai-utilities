"""Loopback OTLP/HTTP capture server for native OTel tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from typing_extensions import Self


def _any_value_to_python(value: Any) -> Any:
    kind = value.WhichOneof("value")
    scalar_attrs = {
        "string_value": "string_value",
        "bool_value": "bool_value",
        "int_value": "int_value",
        "double_value": "double_value",
    }
    if kind in scalar_attrs:
        return getattr(value, scalar_attrs[kind])
    if kind == "bytes_value":
        return bytes(value.bytes_value)
    if kind == "array_value":
        return [_any_value_to_python(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _any_value_to_python(kv.value) for kv in value.kvlist_value.values}
    return None


@dataclass
class CapturedLogRecord:
    body: Any
    severity_text: str
    severity_number: int
    trace_id: str
    span_id: str
    attributes: dict[str, Any]
    resource_attributes: dict[str, Any]
    scope_name: str
    timestamp: int


@dataclass
class CapturedHTTPRequest:
    path: str
    headers: dict[str, str]
    raw_body: bytes
    records: list[CapturedLogRecord] = field(default_factory=list)


class OtlpCaptureServer:
    """In-process loopback HTTP server that decodes OTLP protobuf log batches."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        delay_seconds: float = 0.0,
    ) -> None:
        self.status_code = status_code
        self.delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._requests: list[CapturedHTTPRequest] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    @property
    def requests(self) -> list[CapturedHTTPRequest]:
        with self._lock:
            return list(self._requests)

    def requests_for_path(self, path: str) -> list[CapturedHTTPRequest]:
        with self._lock:
            return [req for req in self._requests if req.path == path]

    @property
    def log_records(self) -> list[CapturedLogRecord]:
        with self._lock:
            return [rec for req in self._requests for rec in req.records]

    def record_request(self, request: CapturedHTTPRequest) -> None:
        with self._lock:
            self._requests.append(request)

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()

    def start(self) -> Self:
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if outer.delay_seconds > 0:
                    time.sleep(outer.delay_seconds)

                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                records: list[CapturedLogRecord] = []

                if self.path.endswith("/v1/logs") and raw:
                    proto_req = ExportLogsServiceRequest()
                    proto_req.ParseFromString(raw)
                    for resource_logs in proto_req.resource_logs:
                        res_attrs = {kv.key: _any_value_to_python(kv.value) for kv in resource_logs.resource.attributes}
                        for scope_logs in resource_logs.scope_logs:
                            scope_name = scope_logs.scope.name
                            for record in scope_logs.log_records:
                                attrs = {kv.key: _any_value_to_python(kv.value) for kv in record.attributes}
                                records.append(
                                    CapturedLogRecord(
                                        body=_any_value_to_python(record.body),
                                        severity_text=record.severity_text,
                                        severity_number=int(record.severity_number),
                                        trace_id=record.trace_id.hex() if record.trace_id else "",
                                        span_id=record.span_id.hex() if record.span_id else "",
                                        attributes=attrs,
                                        resource_attributes=res_attrs,
                                        scope_name=scope_name,
                                        timestamp=int(record.time_unix_nano or record.observed_time_unix_nano),
                                    )
                                )

                outer.record_request(
                    CapturedHTTPRequest(
                        path=self.path,
                        headers=headers,
                        raw_body=raw,
                        records=records,
                    )
                )

                body = (
                    ExportLogsServiceResponse().SerializeToString() if outer.status_code == 200 else b"export failure"
                )
                self.send_response(outer.status_code)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return _Handler
