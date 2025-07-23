from typing import Protocol, TypedDict, runtime_checkable


@runtime_checkable
class LambdaContextLike(Protocol):
    @property
    def aws_request_id(self) -> str: ...

    @property
    def invoked_function_arn(self) -> str: ...


class LambdaContextMetadata(TypedDict):
    request_id: str
    function_arn: str


class ExtractedLambdaContext(TypedDict):
    lambda_context: LambdaContextMetadata


class LambdaEnvironmentMetadata(TypedDict):
    aws_region: str
    function_name: str


class ExtractedLambdaEnvironmentMetadata(TypedDict):
    lambda_os: LambdaEnvironmentMetadata
