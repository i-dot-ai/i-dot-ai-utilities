import json
import os
from typing import Any
from urllib import request
from urllib.parse import urlparse

from i_dot_ai_utilities.logging.enrichers.context_extractor import (
    BaseEnvironmentEnricher,
)
from i_dot_ai_utilities.logging.types.fargate_enrichment_schema import (
    ExtractedFargateContext,
    FargateContainerLabelsLike,
)


class FargateEnvironmentEnricher(BaseEnvironmentEnricher):
    _container_metadata_url_parameter_name: str = "ECS_CONTAINER_METADATA_URI_V4"

    def extract_context(self, self_logger: Any) -> ExtractedFargateContext | None:
        response: ExtractedFargateContext | None = None
        try:
            base_url = self._get_metadata_base_url()

            container_metadata_response = self._get_metadata_response(base_url)
            container_loaded_metadata = FargateContainerMetadataResponse(container_metadata_response)

            task_metadata_response = self._get_metadata_response(f"{base_url}/task")
            task_loaded_metadata = FargateTaskMetadataResponse(task_metadata_response)

            response = {
                "fargate": {
                    "image_id": container_loaded_metadata.image_id,
                    "task_arn": container_loaded_metadata.labels.task_arn,
                    "container_started_at": container_loaded_metadata.started_at,
                    "aws_region": task_loaded_metadata.aws_region,
                }
            }
        except Exception:
            self_logger.exception("Exception(Logger): Failed to extract Fargate container metadata fields")
            return None
        else:
            return response

    def _get_metadata_base_url(self) -> str:
        base_url = os.environ.get(self._container_metadata_url_parameter_name, None)

        if base_url is None:
            msg = "Failed to find metadata URL on environment"
            raise ValueError(msg)

        return base_url

    def _get_metadata_response(self, url: str) -> Any:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            msg = "URL must use HTTP or HTTPS"
            raise ValueError(msg)

        with request.urlopen(parsed_url.geturl()) as response:  # noqa: S310 # nosec B310
            return json.loads(response.read())


class FargateContainerMetadataResponse:
    def __init__(self, raw_response: dict[str, Any]):
        try:
            self.image_id: str = raw_response["ImageID"]
            self.started_at: str = raw_response["StartedAt"]
            self.labels: FargateContainerLabelsLike = FargateContainerLabelsLike(raw_response["Labels"])

        except Exception as e:
            msg = "Exception(Logger): Response doesn't conform to FargateContainerMetadataResponse. Context not set."
            raise TypeError(msg) from e


class FargateTaskMetadataResponse:
    def __init__(self, raw_response: dict[str, Any]):
        try:
            self.aws_region: str = raw_response["AvailabilityZone"][:-1]

        except Exception as e:
            msg = "Exception(Logger): Response doesn't conform to FargateTaskMetadataResponse. Context not set."
            raise TypeError(msg) from e
