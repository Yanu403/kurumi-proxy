from __future__ import annotations

from dataclasses import dataclass

from kurumi_proxy.config import Settings
from kurumi_proxy.db import Connection, ConnectionStore, parse_iso, utc_now
from kurumi_proxy.providers.base import ProviderError, ProviderUnavailableError


@dataclass(frozen=True)
class FailureClassification:
    retryable: bool
    category: str


QUOTA_KEYWORDS = ("quota", "usage_limit", "exhausted", "limit exceeded")
CREDIT_KEYWORDS = ("credit", "insufficient", "balance")
RATE_LIMIT_KEYWORDS = ("rate limit", "429", "too many requests")
AUTH_KEYWORDS = ("unauthorized", "invalid api key", "401", "403", "authentication")
OVERLOAD_KEYWORDS = ("overloaded", "temporarily unavailable", "timeout", "timed out", "unavailable")
TRANSIENT_KEYWORDS = ("bad gateway", "upstream", "connection", "reset", "temporarily")


def classify_provider_error(error: ProviderError) -> FailureClassification:
    message = error.message.lower()
    if any(keyword in message for keyword in QUOTA_KEYWORDS):
        return FailureClassification(True, "quota")
    if any(keyword in message for keyword in CREDIT_KEYWORDS):
        return FailureClassification(True, "credit")
    if any(keyword in message for keyword in AUTH_KEYWORDS):
        return FailureClassification(True, "auth")
    if any(keyword in message for keyword in RATE_LIMIT_KEYWORDS):
        return FailureClassification(True, "rate_limit")
    if any(keyword in message for keyword in OVERLOAD_KEYWORDS):
        return FailureClassification(True, "overload")
    if error.status_code in {502, 503, 504} and any(keyword in message for keyword in TRANSIENT_KEYWORDS):
        return FailureClassification(True, "transient")
    return FailureClassification(False, "non_retryable")


def connection_available(connection: Connection, model: str) -> bool:
    if not connection.is_active:
        return False
    now = utc_now()
    cooldown = parse_iso(connection.cooldown_until)
    if cooldown and cooldown > now:
        return False
    for key in ("__all", model):
        locked_until = parse_iso(connection.model_locks.get(key))
        if locked_until and locked_until > now:
            return False
    return True


def _last_used_key(connection: Connection) -> str:
    return connection.last_used_at or ""


class CredentialRouter:
    def __init__(self, store: ConnectionStore, settings: Settings):
        self.store = store
        self.settings = settings

    def available_connections(self, model: str, exclude_ids: set[str] | None = None) -> list[Connection]:
        exclude_ids = exclude_ids or set()
        connections = [
            connection
            for connection in self.store.list_connections(include_inactive=False)
            if connection.id not in exclude_ids and connection_available(connection, model)
        ]
        strategy = self.settings.kurumi_proxy_routing_strategy
        if strategy == "round-robin":
            limit = self.settings.kurumi_proxy_sticky_round_robin_limit
            connections.sort(key=lambda item: (item.consecutive_use_count >= limit, _last_used_key(item), item.priority, item.name))
        else:
            connections.sort(key=lambda item: (item.priority, _last_used_key(item), item.name))
        return connections

    def next_connection(self, model: str, exclude_ids: set[str] | None = None) -> Connection | None:
        connections = self.available_connections(model, exclude_ids)
        return connections[0] if connections else None

    def mark_success(self, connection: Connection) -> None:
        self.store.mark_success(connection)

    def mark_failure(self, connection: Connection, model: str, error: ProviderError) -> FailureClassification:
        classification = classify_provider_error(error)
        if classification.retryable:
            self.store.mark_failure(connection, model=model, error=error.message, category=classification.category)
        return classification

    def no_credentials_error(self) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            "No active CodeBuddy credentials are available. Add one with POST /admin/connections or set CODEBUDDY_API_KEY."
        )
