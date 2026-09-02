from typing import Any

import httpx
import requests
from pydantic import BaseModel, field_validator

from i_dot_ai_utilities.auth.auth_reason import AuthReason
from i_dot_ai_utilities.auth.exceptions import AuthApiConfigurationError, AuthApiRequestError
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


class AuthApiResponseMetadata(BaseModel):
    user_email: str
    signing_party: str


class AuthApiResponseDecision(BaseModel):
    is_authorised: bool
    auth_reason: AuthReason

    @field_validator("auth_reason", mode="before")
    @classmethod
    def coerce_auth_reason(cls, raw_reason: Any) -> AuthReason:
        try:
            return AuthReason(raw_reason)
        except ValueError:
            return AuthReason.UNKNOWN


class AuthApiResponse(BaseModel):
    metadata: AuthApiResponseMetadata
    decision: AuthApiResponseDecision


class UserAuthorisationResult(BaseModel):
    email: str
    is_authorised: bool
    auth_reason: AuthReason


class AuthApiClient:
    _app_name: str
    _auth_api_url: str
    _logger: StructuredLogger
    _timeout: int
    _async_client: httpx.AsyncClient | None

    def __init__(
        self,
        app_name: str,
        auth_api_url: str,
        logger: StructuredLogger,
        timeout: int = 3,
        async_client: httpx.AsyncClient | None = None,
    ):
        """
        Args:
            async_client:
                An optional long-lived `httpx.AsyncClient` for the async path to reuse.
                If left unset, only the sync path is usable: `aget_user_authorisation_info`
                raises `AuthApiConfigurationError`.
        """
        self._app_name = app_name
        self._auth_api_url = auth_api_url
        self._logger = logger
        self._timeout = timeout
        self._async_client = async_client

    def _process_response_payload(self, response_data: dict[str, Any]) -> UserAuthorisationResult:
        """Common to process response .json()."""

        model = AuthApiResponse.model_validate(response_data)

        self._logger.debug(
            "Auth API decision for {user}. Authorised: {is_authorised}. Reason: {auth_reason}",
            user=model.metadata.user_email,
            is_authorised=model.decision.is_authorised,
            auth_reason=model.decision.auth_reason,
        )

        return UserAuthorisationResult(
            email=model.metadata.user_email,
            is_authorised=model.decision.is_authorised,
            auth_reason=model.decision.auth_reason,
        )

    def get_user_authorisation_info(self, token: str) -> UserAuthorisationResult:
        try:
            endpoint = self._auth_api_url + "/tokens/authorise"

            self._logger.debug("Calling auth api at {url}", url=endpoint)

            payload = {
                "app_name": self._app_name,
                "token": token,
            }

            response: requests.Response = requests.post(endpoint, json=payload, timeout=self._timeout)

            if not response.ok:
                response.raise_for_status()

            return self._process_response_payload(response.json())
        except Exception as e:
            self._logger.exception("Auth API request failed")
            raise AuthApiRequestError from e

    async def aget_user_authorisation_info(self, token: str) -> UserAuthorisationResult:
        """
        Requires the `async_client` given to the constructor, whose connection pool stays warm
        between calls.

        Raises:
            AuthApiConfigurationError: If the client was constructed without an `async_client`.
        """
        if not self._async_client:
            msg = "aget_user_authorisation_info requires an async_client to be passed to AuthApiClient."
            raise AuthApiConfigurationError(msg)

        try:
            endpoint = self._auth_api_url + "/tokens/authorise"

            self._logger.debug("Calling auth api at {url}", url=endpoint)

            payload = {
                "app_name": self._app_name,
                "token": token,
            }

            response: httpx.Response = await self._async_client.post(endpoint, json=payload)

            response.raise_for_status()
            return self._process_response_payload(response.json())

        except Exception as e:
            self._logger.exception("Auth API request failed")
            raise AuthApiRequestError from e
