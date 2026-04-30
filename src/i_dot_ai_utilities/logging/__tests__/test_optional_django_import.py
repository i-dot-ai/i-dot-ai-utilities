# mypy: disable-error-code="no-untyped-def"
"""Tests verifying the library's soft-Django-dependency contract.

The constitution requires that:

- ``i_dot_ai_utilities.logging`` and ``i_dot_ai_utilities.logging.middleware``
  are importable in an environment without Django.
- The Django-specific module ``i_dot_ai_utilities.logging.middleware.django``
  does not execute any Django imports at module-load time (imports are lazy
  inside ``__init__`` / ``__call__``), so importing it in a Django-free env is
  tolerated; instantiating it without Django fails cleanly with an ImportError
  mentioning ``django``.

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
        "from i_dot_ai_utilities.logging.middleware._trace import parse_traceparent\n"
        "from i_dot_ai_utilities.logging.middleware._exclusions import ExclusionMatcher\n"
        "from i_dot_ai_utilities.logging.middleware._levels import level_for_status\n"
        "from i_dot_ai_utilities.logging.middleware._headers import TRACEPARENT\n"
        "from i_dot_ai_utilities.logging.middleware._settings import resolve_logger\n"
        "print('OK')\n"
    )
    script = _DJANGO_BLOCK_SETUP + body
    result = _run_child(script)
    assert result.returncode == 0, f"import failed. stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_middleware_django_module_requires_django_to_import():
    """Importing the Django-specific submodule fails cleanly without Django.

    Django imports live at module top level in ``middleware/django.py`` — this
    is the intended design (see anti-patterns brief §F.3: lazy imports inside
    ``__call__`` obscure the dependency graph and buy nothing when the
    containing module is by definition Django-specific). Importing the module
    without Django must therefore surface an ImportError mentioning ``django``,
    not a confusing error from our own package.
    """
    body = (
        "try:\n"
        "    import i_dot_ai_utilities.logging.middleware.django  # noqa: F401\n"
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
        logging_root / "middleware" / "django.py",
        # OTel-backed alternative middleware; same design intent as
        # middleware/django.py — Django imports at module top level are
        # allowed because this module is by definition Django-specific.
        logging_root / "middleware" / "django_otel.py",
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
    assert not offenders, "Top-level Django imports outside middleware/django.py: " + "; ".join(offenders)
