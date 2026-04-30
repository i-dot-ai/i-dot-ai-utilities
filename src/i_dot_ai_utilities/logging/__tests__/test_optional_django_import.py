# mypy: disable-error-code="no-untyped-def"
"""Tests verifying the library's soft-Django-dependency contract.

The constitution requires that:

- ``i_dot_ai_utilities.logging`` and ``i_dot_ai_utilities.logging.middleware``
  are importable in an environment without Django.
- The Django-specific modules (``middleware.django_otel`` and
  ``middleware.django_user_id``) do not execute any Django imports at
  module-load time in a way that would crash pure-Python imports of the
  rest of the library; importing them in a Django-free env surfaces an
  ImportError mentioning ``django`` cleanly.

We simulate "Django not installed" by running child Python subprocesses that
inject a ``sys.meta_path`` finder blocking any import that targets Django.
Uses the modern ``find_spec`` protocol so the block is enforced under
Python 3.12+ (where legacy ``find_module`` is a no-op).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

_DJANGO_BLOCK_SETUP = (
    "import sys\n"
    "class _BlockDjango:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'django' or name.startswith('django.'):\n"
    "            raise ImportError('No module named ' + name + ' (blocked for test)')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _BlockDjango())\n"
    "for k in list(sys.modules):\n"
    "    if k == 'django' or k.startswith('django.'):\n"
    "        del sys.modules[k]\n"
)


def _run_child(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_logging_package_and_middleware_subpackage_importable_without_django():
    """Root logging imports must work without Django installed."""
    body = (
        "try:\n"
        "    import django\n"
        "    raise SystemExit('block failed: django importable')\n"
        "except ImportError:\n"
        "    pass\n"
        "import i_dot_ai_utilities\n"
        "import i_dot_ai_utilities.logging\n"
        "import i_dot_ai_utilities.logging.middleware\n"
        "from i_dot_ai_utilities.logging.structured_logger import StructuredLogger\n"
        "from i_dot_ai_utilities.logging.middleware._exclusions import ExclusionMatcher\n"
        "from i_dot_ai_utilities.logging.middleware._levels import level_for_status\n"
        "from i_dot_ai_utilities.logging.middleware._headers import X_REQUEST_ID, validate_request_id\n"
        "from i_dot_ai_utilities.logging.middleware._settings import resolve_logger\n"
        "print('OK')\n"
    )
    script = _DJANGO_BLOCK_SETUP + body
    result = _run_child(script)
    assert result.returncode == 0, f"import failed. stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_middleware_django_otel_module_requires_django_to_import():
    """Importing ``middleware.django_otel`` fails cleanly without Django.

    Django imports live at module top level in the Django-specific
    submodules — this is the intended design (see anti-patterns brief §F.3:
    lazy imports inside ``__call__`` obscure the dependency graph and buy
    nothing when the containing module is by definition Django-specific).
    Importing either of them without Django must therefore surface an
    ImportError mentioning ``django``, not a confusing error from our own
    package.
    """
    body = (
        "try:\n"
        "    import i_dot_ai_utilities.logging.middleware.django_otel  # noqa: F401\n"
        "except ImportError as exc:\n"
        "    if 'django' not in str(exc).lower():\n"
        "        raise SystemExit('unexpected error: ' + str(exc))\n"
        "    print('OK')\n"
        "else:\n"
        "    raise SystemExit('import succeeded without Django')\n"
    )
    script = _DJANGO_BLOCK_SETUP + body
    result = _run_child(script)
    assert result.returncode == 0, f"unexpected behaviour. stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_middleware_django_user_id_module_requires_django_to_import():
    body = (
        "try:\n"
        "    import i_dot_ai_utilities.logging.middleware.django_user_id  # noqa: F401\n"
        "except ImportError as exc:\n"
        "    if 'django' not in str(exc).lower():\n"
        "        raise SystemExit('unexpected error: ' + str(exc))\n"
        "    print('OK')\n"
        "else:\n"
        "    raise SystemExit('import succeeded without Django')\n"
    )
    script = _DJANGO_BLOCK_SETUP + body
    result = _run_child(script)
    assert result.returncode == 0, f"unexpected behaviour. stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_no_django_imports_at_top_of_logging_modules():
    """Defence in depth: verify source files do not grep-positive for top-level
    Django imports outside the Django-specific submodules.
    """
    logging_root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {
        # OTel-backed Django middleware; Django imports at module top level
        # are allowed because this module is by definition Django-specific.
        logging_root / "middleware" / "django_otel.py",
        # Thin middleware that binds user.id onto the log context; same
        # design intent.
        logging_root / "middleware" / "django_user_id.py",
    }

    offenders: list[str] = []
    for py in logging_root.rglob("*.py"):
        if py in allowed:
            continue
        if "__tests__" in py.parts:
            continue
        for line in py.read_text().splitlines():
            if line.startswith(("import django", "from django")):
                offenders.append(f"{py}: {line!r}")
    assert not offenders, "Top-level Django imports outside Django-specific modules: " + "; ".join(offenders)
