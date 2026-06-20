"""Provider abstractions.

A *provider* is a backend that can turn an OpenAI-style chat completion
request into a completion. Every provider implements :class:`BaseProvider`.

The proxy currently uses :class:`MerlinProvider` as the sole backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from kurumi_proxy.models import ChatMessage


@dataclass
class ProviderResult:
    """Result of a non-streaming completion.

    ``stream`` may yield these incrementally (see :meth:`BaseProvider.stream`).
    """

    text: str
    model: str
    tool_calls: list | None = None
    reasoning_content: str | None = None
    finish_reason: str = "stop"


@dataclass
class StreamDelta:
    """A single chunk yielded by :meth:`BaseProvider.stream`."""

    text: str = ""
    model: str | None = None
    tool_calls: list | None = None
    reasoning_content: str | None = None
    finish_reason: str | None = None
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    """Base class for all provider failures.

    The ``status_code`` attribute maps the failure onto an HTTP status that
    the proxy returns to the caller.
    """

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


class ProviderRateLimitError(ProviderError):
    """Upstream throttled us (HTTP 429 / quota exhausted)."""

    status_code = 429


class ProviderAuthError(ProviderError):
    """Upstream rejected our credentials (HTTP 401/403)."""

    status_code = 502


class BaseProvider(ABC):
    """Abstract base class for all providers.

    Subclasses must implement :meth:`complete`. :meth:`stream` has a default
    implementation that delegates to :meth:`complete` and emits a single
    chunk; providers with native streaming override it.
    """

    #: Short identifier used in usage tracking.
    provider_id: str = "base"

    #: Human-readable name.
    provider_name: str = "Base Provider"

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> ProviderResult:
        """Return a full completion for ``messages``."""

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Yield completion chunks.

        The default implementation calls :meth:`complete` once and emits a
        single delta with the full text followed by a terminal chunk. This
        lets non-streaming providers participate in streaming responses
        without extra work.
        """
        result = await self.complete(messages, model, api_key=api_key)
        if result.text:
            yield StreamDelta(text=result.text, model=result.model)
        yield StreamDelta(model=result.model, finish_reason=result.finish_reason or "stop")

    def list_models(self) -> list[str]:
        """Return the model IDs this provider serves (without the prefix)."""
        return []
