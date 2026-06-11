from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderResult:
    text: str
    model: str


class ProviderError(Exception):
    status_code = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class MissingCredentialError(ProviderError):
    status_code = 503


class ProviderUnavailableError(ProviderError):
    status_code = 503


class ProviderBadGatewayError(ProviderError):
    status_code = 502


class ProviderTimeoutError(ProviderError):
    status_code = 504
