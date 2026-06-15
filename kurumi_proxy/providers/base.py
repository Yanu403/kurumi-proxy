from dataclasses import dataclass

@dataclass
class ProviderResult:
    text: str
    model: str
    tool_calls: list | None = None
    reasoning_content: str | None = None
    finish_reason: str = "stop"

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
