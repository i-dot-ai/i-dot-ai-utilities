"""Tests for OpenTelemetry semantic convention and ``i_ai.*`` attribute naming rules and enforcement."""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.sdk._logs import LogRecordLimits, ReadWriteLogRecord

from i_dot_ai_utilities.otel import I_AI_NAMESPACE, iai_attribute, validate_attributes
from i_dot_ai_utilities.otel._attributes import (
    _SDK_LOG_ATTRIBUTES,
    ENV_STRICT_ATTRIBUTES,
    AttributeNamespaceProcessor,
    _semconv_registry,
    is_valid_attribute_name,
)


class TestAttributeNameValidation:
    """Every attribute sent to ``otel-gateway`` must be an OTel semantic convention or ``i_ai.<service>.<property>``."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("i_ai.auth.decision.reason", id="i_ai-three-levels"),
            pytest.param("i_ai.auth.token.key_id", id="i_ai-snake-case-segment"),
            pytest.param("i_ai.auth.application", id="i_ai-two-levels-minimum"),
            pytest.param("user.email", id="semconv-user-attribute"),
            pytest.param("url.full", id="semconv-url-attribute"),
            pytest.param("error.type", id="semconv-error-attribute"),
            pytest.param("code.file.path", id="semconv-code-attribute"),
            pytest.param("exception.stacktrace", id="semconv-exception-attribute"),
            pytest.param("aws.ecs.task.arn", id="semconv-aws-resource-attribute"),
            pytest.param("http.request.header.x_request_id", id="semconv-template-underscore-key"),
            pytest.param("http.request.header.content-type", id="semconv-template-hyphenated-key"),
        ],
    )
    def test_accepts_compliant_attribute_name(self, name: str) -> None:
        assert is_valid_attribute_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("application", id="bare-unnamespaced-key"),
            pytest.param("user.my_field", id="custom-key-under-otel-namespace"),
            pytest.param("i_ai.auth", id="i_ai-missing-property-level"),
            pytest.param("i_ai.auth.tokenKeyId", id="i_ai-camel-case-segment"),
            pytest.param("i_ai.Auth.reason", id="i_ai-uppercase-segment"),
            pytest.param("i_ai..auth.reason", id="i_ai-empty-segment"),
            pytest.param("iai.auth.reason", id="misspelled-iai-prefix"),
            pytest.param("http.request.header.", id="semconv-template-empty-key"),
        ],
    )
    def test_rejects_non_compliant_attribute_name(self, name: str) -> None:
        assert not is_valid_attribute_name(name)


class TestAttributeHelpers:
    """Public helpers catch bad attribute names at import time, and source parsing stays faithful to semconv."""

    def test_iai_attribute_prefixes_valid_name_and_rejects_malformed_names(self) -> None:
        assert I_AI_NAMESPACE == "i_ai"
        assert iai_attribute("auth.decision.reason") == "i_ai.auth.decision.reason"
        for bad in ("auth", "Auth.reason", "auth.tokenKeyId", "auth..reason", ""):
            with pytest.raises(ValueError, match="Invalid i_ai attribute"):
                iai_attribute(bad)

    def test_validate_attributes_returns_dict_or_lists_every_invalid_key(self) -> None:
        assert validate_attributes({"user.email": "a@b.c"}) == {"user.email": "a@b.c"}
        with pytest.raises(ValueError, match=r"\['application', 'user.my_field'\]"):
            validate_attributes({"user.my_field": 1, "application": "x", "url.full": "y"})

    def test_source_parsed_semconv_registry_matches_imported_modules(self) -> None:
        """``_semconv_registry`` scans source files to avoid ~300 ms of cold-start imports; fail if format drifts."""
        imported: set[str] = set()
        for package_name in ("opentelemetry.semconv.attributes", "opentelemetry.semconv._incubating.attributes"):
            package = importlib.import_module(package_name)
            for module_info in pkgutil.iter_modules(package.__path__):
                module = importlib.import_module(f"{package_name}.{module_info.name}")
                imported |= {v for k, v in vars(module).items() if k.isupper() and isinstance(v, str)}
        names, templates = _semconv_registry()
        assert len(names) > 500
        assert names >= _SDK_LOG_ATTRIBUTES
        assert names | {t.removesuffix(".") for t in templates} == imported


class TestAttributeNamespaceProcessor:
    """Runtime processor renames bad keys without dropping data, or raises in strict CI mode."""

    @staticmethod
    def _emit(processor: AttributeNamespaceProcessor, attributes: dict[str, object]) -> dict[str, object]:
        record = ReadWriteLogRecord(LogRecord(body="m", attributes=attributes))  # type: ignore[arg-type]
        processor.on_emit(record)
        return dict(record.log_record.attributes or {})

    def test_renames_non_compliant_keys_under_unnamespaced_prefix_and_warns_once(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        processor = AttributeNamespaceProcessor()
        first = self._emit(processor, {"mode": "ok", "i_ai.auth.application": "x", "code.line.number": 3})
        second = self._emit(processor, {"mode": "again"})

        assert first == {"i_ai.auth.application": "x", "code.line.number": 3, "i_ai.unnamespaced.mode": "ok"}
        assert second == {"i_ai.unnamespaced.mode": "again"}
        assert capsys.readouterr().err.count("log attribute 'mode'") == 1

    def test_maps_lambda_aws_request_id_to_faas_invocation_id_and_drops_empty_value(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``awslambdaric`` stamps ``aws_request_id`` onto every record, empty outside an invocation."""
        processor = AttributeNamespaceProcessor()
        assert self._emit(processor, {"aws_request_id": "req-1"}) == {"faas.invocation_id": "req-1"}
        assert self._emit(processor, {"aws_request_id": ""}) == {}
        assert capsys.readouterr().err == ""

    def test_strict_mode_raises_value_error_on_emit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_STRICT_ATTRIBUTES, "true")
        processor = AttributeNamespaceProcessor()
        with pytest.raises(ValueError, match="application"):
            self._emit(processor, {"application": "x"})

    def test_strict_mode_rewrites_lambda_aws_request_id_before_validating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``awslambdaric`` injects ``aws_request_id`` automatically, so strict mode must map it before checking."""
        monkeypatch.setenv(ENV_STRICT_ATTRIBUTES, "true")
        processor = AttributeNamespaceProcessor()
        result = self._emit(processor, {"aws_request_id": "req-42", "i_ai.auth.application": "casework"})
        assert result == {"faas.invocation_id": "req-42", "i_ai.auth.application": "casework"}

    def test_strict_mode_renames_third_party_framework_logger_keys_instead_of_raising(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Framework loggers such as ``django.request`` attach unnamespaced extra keys outside app control."""
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope  # noqa: PLC0415

        monkeypatch.setenv(ENV_STRICT_ATTRIBUTES, "true")
        processor = AttributeNamespaceProcessor()
        record = ReadWriteLogRecord(
            LogRecord(body="Not Found: /missing", attributes={"status_code": 404}),
            instrumentation_scope=InstrumentationScope("django.request"),
        )
        processor.on_emit(record)
        assert dict(record.log_record.attributes or {}) == {"i_ai.unnamespaced.status_code": 404}

    def test_deletes_invalid_key_before_renaming_so_bounded_attributes_do_not_evict_valid_keys(self) -> None:
        """``BoundedAttributes`` evicts the oldest entry on insert when full; deleting first preserves valid keys."""
        limits = LogRecordLimits(max_attributes=2)
        record = ReadWriteLogRecord(
            LogRecord(body="m", attributes={"code.line.number": 10, "bad_key": "v"}),
            limits=limits,
        )
        processor = AttributeNamespaceProcessor()
        processor.on_emit(record)
        assert dict(record.log_record.attributes or {}) == {
            "code.line.number": 10,
            "i_ai.unnamespaced.bad_key": "v",
        }
        assert processor.force_flush() is True
        processor.shutdown()

    def test_strict_mode_raises_end_to_end_even_when_otlp_export_is_disabled(self) -> None:
        """Consumer test suites disable OTLP export and rely on strict mode to catch bad attribute names in CI."""
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "configure_otel()\n"
            "try:\n"
            "    logging.getLogger('app').info('bad', extra={'application': 'casework'})\n"
            "except ValueError as exc:\n"
            "    print('STRICT_RAISED', exc)\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "I_DOT_AI_OTEL_", "AWS_LAMBDA_"))}
        env.update({"OTEL_LOGS_EXPORTER": "none", "I_DOT_AI_OTEL_STRICT_ATTRIBUTES": "true"})
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, f"stderr={proc.stderr!r}"
        assert "STRICT_RAISED" in proc.stdout
        assert "'application'" in proc.stdout
