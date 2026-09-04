# mypy: disable-error-code="no-untyped-def"
"""Contract tests for the public ``i_dot_ai_utilities.logging.otel`` alias.

The optionality half (base package imports without the ``[otel]`` extra, the
alias fails cleanly without it) is the whole reason the implementation sits
behind ``_otel``; it mirrors ``test_optional_django_import``.
"""

from __future__ import annotations

import subprocess
import sys

from i_dot_ai_utilities.logging import _otel
from i_dot_ai_utilities.logging import otel as public_otel

_OTEL_BLOCK_SETUP = (
    "import sys\n"
    "class _BlockOtel:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'opentelemetry' or name.startswith('opentelemetry.'):\n"
    "            raise ImportError('No module named ' + name + ' (blocked for test)')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _BlockOtel())\n"
    "for k in list(sys.modules):\n"
    "    if k == 'opentelemetry' or k.startswith('opentelemetry.'):\n"
    "        del sys.modules[k]\n"
)


def _run_child(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_public_module_re_exports_internal_surface():
    assert public_otel.__all__ == _otel.__all__
    for name in _otel.__all__:
        assert getattr(public_otel, name) is getattr(_otel, name)


def test_base_logging_package_importable_without_otel():
    body = (
        "import i_dot_ai_utilities.logging\n"
        "from i_dot_ai_utilities.logging.structured_logger import StructuredLogger\n"
        "print('OK')\n"
    )
    result = _run_child(_OTEL_BLOCK_SETUP + body)
    assert result.returncode == 0, f"import failed. stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_public_otel_module_requires_otel_to_import():
    body = (
        "try:\n"
        "    import i_dot_ai_utilities.logging.otel  # noqa: F401\n"
        "except ImportError as exc:\n"
        "    if 'opentelemetry' not in str(exc).lower():\n"
        "        raise SystemExit('unexpected error: ' + str(exc))\n"
        "    print('OK')\n"
        "else:\n"
        "    raise SystemExit('import succeeded without opentelemetry')\n"
    )
    result = _run_child(_OTEL_BLOCK_SETUP + body)
    assert result.returncode == 0, f"unexpected behaviour. stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout
