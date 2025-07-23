from typing import Literal, TypedDict


class BaseContextEnvVars(TypedDict):
    app_name: str
    repo_name: str
    environment_name: str


class BaseContext(TypedDict):
    context_id: str
    env: BaseContextEnvVars
    ship_logs: Literal[1, 0]
