from typing import TypedDict


class FargateContainerLabelsLike:
    def __init__(self, raw_labels: dict[str, str]):
        self._raw = raw_labels

    def __getitem__(self, key: str) -> str:
        return self._raw[key]

    @property
    def task_arn(self) -> str:
        return self._raw.get("com.amazonaws.ecs.task-arn", "unknown")


ExtractedFargateContext = TypedDict(
    "ExtractedFargateContext",
    {
        "fargate.image_id": str,
        "fargate.task_arn": str,
        "fargate.container_started_at": str,
    },
)
