from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kurumi_proxy.config import Settings
from kurumi_proxy.models import ChatMessage, GenericContentBlock, TextContentBlock


@dataclass(frozen=True)
class RtkStats:
    before_bytes: int = 0
    after_bytes: int = 0
    saved_bytes: int = 0


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _looks_error(text: str) -> bool:
    lower = text.lower()
    return "traceback" in lower or "exception" in lower or "error:" in lower or "is_error=true" in lower


def _dedupe_repeated_lines(lines: list[str]) -> list[str]:
    if len(lines) < 20:
        return lines
    result: list[str] = []
    previous: str | None = None
    repeated = 0
    for line in lines:
        if line == previous:
            repeated += 1
            continue
        if repeated:
            result.append(f"[kurumi-proxy rtk-lite: previous line repeated {repeated} times]")
            repeated = 0
        result.append(line)
        previous = line
    if repeated:
        result.append(f"[kurumi-proxy rtk-lite: previous line repeated {repeated} times]")
    return result


def compress_text(text: str, settings: Settings, *, preserve_errors: bool = True) -> tuple[str, RtkStats]:
    before = _bytes(text)
    if before < settings.kurumi_proxy_rtk_min_bytes:
        return text, RtkStats(before, before, 0)
    if preserve_errors and _looks_error(text):
        return text, RtkStats(before, before, 0)

    if before > settings.kurumi_proxy_rtk_max_bytes:
        text = text.encode("utf-8")[: settings.kurumi_proxy_rtk_max_bytes].decode("utf-8", errors="ignore")

    lines = _dedupe_repeated_lines(text.splitlines())
    head_count = settings.kurumi_proxy_rtk_head_lines
    tail_count = settings.kurumi_proxy_rtk_tail_lines
    if len(lines) <= head_count + tail_count + 1:
        candidate = "\n".join(lines)
    else:
        omitted = len(lines) - head_count - tail_count
        marker = f"[kurumi-proxy rtk-lite: truncated {omitted} lines / {before} bytes, preserved head/tail]"
        candidate = "\n".join([*lines[:head_count], marker, *lines[-tail_count:]])

    if not candidate.strip():
        candidate = text[: max(1, settings.kurumi_proxy_rtk_min_bytes)]
    after = _bytes(candidate)
    if after >= before:
        return text, RtkStats(before, before, 0)
    return candidate, RtkStats(before, after, before - after)


def _combine(a: RtkStats, b: RtkStats) -> RtkStats:
    return RtkStats(a.before_bytes + b.before_bytes, a.after_bytes + b.after_bytes, a.saved_bytes + b.saved_bytes)


def _compress_content_value(value: Any, settings: Settings) -> tuple[Any, RtkStats]:
    if isinstance(value, str):
        return compress_text(value, settings)
    if isinstance(value, list):
        stats = RtkStats()
        new_values: list[Any] = []
        for item in value:
            new_item, item_stats = _compress_content_value(item, settings)
            stats = _combine(stats, item_stats)
            new_values.append(new_item)
        return new_values, stats
    if isinstance(value, dict):
        copied = dict(value)
        stats = RtkStats()
        if "content" in copied:
            copied["content"], stats = _compress_content_value(copied["content"], settings)
        return copied, stats
    return value, RtkStats()


def preprocess_messages(messages: list[ChatMessage], settings: Settings) -> tuple[list[ChatMessage], RtkStats]:
    if not settings.kurumi_proxy_rtk_enabled:
        return messages, RtkStats()

    stats = RtkStats()
    processed: list[ChatMessage] = []
    for message in messages:
        role = message.role.lower()
        should_compress = role in {"tool", "tool_result"}
        content = message.content
        if should_compress and isinstance(content, str):
            new_content, item_stats = compress_text(content, settings)
            stats = _combine(stats, item_stats)
            processed.append(message.model_copy(update={"content": new_content}))
            continue
        if isinstance(content, list):
            changed_blocks = []
            block_stats = RtkStats()
            for block in content:
                if isinstance(block, TextContentBlock):
                    if should_compress:
                        new_text, item_stats = compress_text(block.text, settings)
                        block_stats = _combine(block_stats, item_stats)
                        changed_blocks.append(block.model_copy(update={"text": new_text}))
                    else:
                        changed_blocks.append(block)
                elif isinstance(block, GenericContentBlock) and block.type == "tool_result":
                    raw = block.model_dump()
                    new_raw, item_stats = _compress_content_value(raw, settings)
                    block_stats = _combine(block_stats, item_stats)
                    changed_blocks.append(GenericContentBlock.model_validate(new_raw))
                else:
                    changed_blocks.append(block)
            if block_stats.before_bytes:
                stats = _combine(stats, block_stats)
                processed.append(message.model_copy(update={"content": changed_blocks}))
            else:
                processed.append(message)
            continue
        processed.append(message)
    return processed, stats
