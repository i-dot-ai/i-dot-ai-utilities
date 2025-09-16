from pydantic import BaseModel
import requests

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger


class AuthApiResponseMetadata(BaseModel):
    user_email: str
    signing_party: str


class AuthApiResponseDecision(BaseModel):
    is_authorised: bool
    auth_reason: str


class AuthApiResponse(BaseModel):
    metadata: AuthApiResponseMetadata
    decision: AuthApiResponseDecision


class UserAuthorisationResult(BaseModel):
    email: str
    is_authorised: bool


class AuthApiClient:
    _app_name: str
    _auth_api_url: str
    _logger: StructuredLogger

    def __init__(self, app_name: str, auth_api_url: str, logger: StructuredLogger):
        self._app_name = app_name
        self._auth_api_url = auth_api_url
        self._logger = logger

    def get_user_authorisation_info(self, token: str) -> UserAuthorisationResult:
        try:
            payload = {
                "token": token,
            }

            self._logger.debug("Calling auth api at {url}", url=self._auth_api_url)

            response = requests.post(self._auth_api_url, json=payload)

            if not response.ok:
                response.raise_for_status()

            data = response.json()

            model = AuthApiResponse.model_validate(data)

            self._logger.debug(
                "Auth API decision for {user}. Authorised: {is_authorised}. Reason: {auth_reason}",
                user=model.metadata.user_email,
                is_authorised=model.decision.is_authorised,
                auth_reason=model.decision.auth_reason,
            )

            return UserAuthorisationResult(email=model.metadata.user_email, is_authorised=model.decision.is_authorised)
        except Exception as e:
            self._logger.exception("Auth API request failed")
            raise AuthApiRequestError from e
