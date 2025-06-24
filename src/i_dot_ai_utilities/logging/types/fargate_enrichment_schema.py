from typing import TypedDict


class FargateContainerLabelsLike:
    def __init__(self, raw_labels: dict[str, str]):
        self._raw = raw_labels

    def __getitem__(self, key: str) -> str:
        return self._raw[key]

    @property
    def task_arn(self) -> str:
        return self._raw.get("com.amazonaws.ecs.task-arn", "unknown")


class ExtractedFargateContext(TypedDict):
    fargate_image_id: str
    fargate_task_arn: str
    fargate_container_started_at: str
