from typing import Any, Protocol, TypedDict, runtime_checkable


@runtime_checkable
class ResolverMatchLike(Protocol):
    @property
    def view_name(self) -> str | None: ...

    @property
    def url_name(self) -> str | None: ...


class DjangoRequestLike(Protocol):
    """Minimal duck-typed shape of a ``django.http.HttpRequest``.

    Defined as a ``Protocol`` (no ``@runtime_checkable``) so static type
    checkers can validate callers while the enricher enforces actual
    identity with ``isinstance(request, django.http.HttpRequest)`` at
    runtime (see security finding A1). Runtime-checkable Protocols only
    verify attribute presence, which lets any object with the right
    attribute names spoof the contract — unsuitable as an integrity check
    for an observability path that logs authenticated user IDs.

    ``headers`` requires Django >= 2.2 (the case-insensitive ``HttpHeaders``
    mapping was introduced in that release). All currently-supported Django
    versions provide it.
    """

    @property
    def method(self) -> str | None: ...

    @property
    def path(self) -> str: ...

    @property
    def scheme(self) -> str: ...

    @property
    def META(self) -> dict[str, Any]: ...  # noqa: N802 - matches Django attribute name

    @property
    def GET(self) -> Any: ...  # noqa: N802 - matches Django attribute name

    @property
    def headers(self) -> Any: ...

    def get_host(self) -> str: ...


# ``ExtractedDjangoContext`` uses dotted OpenTelemetry semantic-convention
# field names (e.g. ``http.request.method``). Dotted keys require the
# functional ``TypedDict`` constructor. ``total=False`` marks all fields as
# optional so the enricher can omit absent values from the log line.
#
# Note: ``user.id`` is the canonical OTel attribute. The older ``enduser.*``
# namespace was deprecated in OTel semconv v1.24 and is no longer emitted.
# Downstream consumers migrating from 0.x may need to update their queries.
ExtractedDjangoContext = TypedDict(
    "ExtractedDjangoContext",
    {
        # Always emitted when the request object is well-formed.
        "http.request.method": str,
        "url.scheme": str,
        "url.path": str,
        "url.query": str,
        "server.address": str,
        # Conditionally emitted — omitted entirely when the source is absent.
        "user_agent.original": str,
        "client.address": str,
        "http.request.header.x_forwarded_for": str,
        "http.route": str,
        "django.url_name": str,
        "user.id": str,
    },
    total=False,
)
