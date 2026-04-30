"""Django integration middleware for the structured logger.

This subpackage holds Django-specific code. The library treats Django as a
soft optional dependency: install with ``pip install "i-dot-ai-utilities[django]"``.

Only the concrete Django modules (``i_dot_ai_utilities.logging.middleware.django_otel``
and ``i_dot_ai_utilities.logging.middleware.django_user_id``) import Django.
The helpers in this subpackage (``_headers``, ``_exclusions``, ``_levels``,
``_settings``) are pure Python and safe to import in non-Django consumers.
"""
