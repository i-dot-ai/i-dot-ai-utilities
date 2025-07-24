class MiscellaneousLiteLLMError(Exception):
    """Exception raised for a generic error occurring during usage of LiteLLM functions."""

    def __init__(self, message: str, error_code: int):
        super().__init__(message)
        self.error_code = error_code
        self.message = message

    def __str__(self):  # type: ignore[no-untyped-def]
        return f"{self.message} (Error Code: {self.error_code})"


class ModelNotAvailableError(Exception):
    """Exception raised for a given model being unavailable in LiteLLM."""

    def __init__(self, message: str, error_code: int):
        super().__init__(message)
        self.error_code = error_code
        self.message = message

    def __str__(self):  # type: ignore[no-untyped-def]
        return f"{self.message} (Error Code: {self.error_code})"
