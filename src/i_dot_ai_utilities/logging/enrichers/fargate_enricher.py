import json
import os
from typing import Any
from urllib import request
from urllib.parse import urlparse

from i_dot_ai_utilities.logging.types.fargate_enrichment_schema import (
    ExtractedFargateContext,
    FargateContainerLabelsLike,
)


class FargateEnvironmentEnricher:
    _container_metadata_url_parameter_name: str = "ECS_CONTAINER_METADATA_URI_V4"

    def extract_context(self, self_logger: Any) -> ExtractedFargateContext | None:
        try:
            metadata_response = self._get_metadata_response()
            loaded_metadata = FargateContainerMetadataResponse(metadata_response)
        except Exception:
            self_logger.exception(
                "Exception(Logger): Failed to extract Fargate container metadata fields"
            )
            return None

        return {
            "fargate.image_id": loaded_metadata.image_id,
            "fargate.task_arn": loaded_metadata.labels.task_arn,
            "fargate.container_started_at": loaded_metadata.started_at,
        }

    def _get_metadata_response(self) -> Any:
        url = os.environ.get(self._container_metadata_url_parameter_name, None)

        if url is None:
            msg = "Failed to find metadata URL on environment"
            raise ValueError(msg)

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
            self.labels: FargateContainerLabelsLike = FargateContainerLabelsLike(
                raw_response["Labels"]
            )
        except Exception as e:
            msg = (
                "Exception(Logger): Response doesn't conform to "
                "FargateContainerMetadataResponse. Context not set."
            )
            raise TypeError(msg) from e
