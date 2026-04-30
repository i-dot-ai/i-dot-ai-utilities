"""Django integration middleware for the structured logger.

This subpackage holds Django-specific code. The library treats Django as a
soft optional dependency: install with ``pip install "i-dot-ai-utilities[django]"``.

Only the concrete Django module (``i_dot_ai_utilities.logging.middleware.django``)
imports Django. The helpers in this subpackage (``_trace``, ``_headers``,
``_exclusions``, ``_levels``, ``_settings``) are pure Python and safe to import
in non-Django consumers.
"""
