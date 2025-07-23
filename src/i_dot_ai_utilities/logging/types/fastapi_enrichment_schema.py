from typing import Any, Protocol, TypedDict, runtime_checkable


@runtime_checkable
class URLLike(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def query(self) -> str: ...


@runtime_checkable
class RequestLike(Protocol):
    @property
    def method(self) -> str: ...

    @property
    def path_params(self) -> dict[str, Any]: ...

    @property
    def base_url(self) -> Any: ...

    @property
    def headers(self) -> Any: ...

    @property
    def url(self) -> URLLike: ...


class FastApiRequestMetadata(TypedDict):
    method: str
    base_url: str
    user_agent: str
    x_forwarded_for: str
    path: str
    query: str


class ExtractedFastApiContext(TypedDict):
    request: FastApiRequestMetadata
