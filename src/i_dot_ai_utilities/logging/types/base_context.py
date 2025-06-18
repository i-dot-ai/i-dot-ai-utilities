from typing import Literal, TypedDict


class BaseContext(TypedDict):
    context_id: str
    env_app_name: str
    env_environment_name: str
    ship_logs: Literal[1] | Literal[0]
