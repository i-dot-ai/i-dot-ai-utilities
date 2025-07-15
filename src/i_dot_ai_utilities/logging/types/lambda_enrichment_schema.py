from typing import Protocol, TypedDict, runtime_checkable


@runtime_checkable
class LambdaContextLike(Protocol):
    @property
    def function_name(self) -> str: ...

    @property
    def aws_request_id(self) -> str: ...

    @property
    def invoked_function_arn(self) -> str: ...


class LambdaContextMetadata(TypedDict):
    function_name: str
    request_id: str
    function_arn: str


class ExtractedLambdaContext(TypedDict):
    lambda_context: LambdaContextMetadata
