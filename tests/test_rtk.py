from kurumi_proxy.config import Settings
from kurumi_proxy.models import ChatMessage
from kurumi_proxy.rtk import compress_text, preprocess_messages


def test_rtk_compresses_large_tool_output() -> None:
    settings = Settings(
        _env_file=None,
        KURUMI_PROXY_RTK_MIN_BYTES=100,
        KURUMI_PROXY_RTK_HEAD_LINES=2,
        KURUMI_PROXY_RTK_TAIL_LINES=2,
    )
    text = "\n".join(f"line {index}" for index in range(100))

    compressed, stats = compress_text(text, settings)

    assert len(compressed) < len(text)
    assert stats.saved_bytes > 0
    assert "kurumi-proxy rtk-lite" in compressed
    assert "line 0" in compressed
    assert "line 99" in compressed


def test_rtk_preserves_small_and_error_content() -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_RTK_MIN_BYTES=50)

    small, small_stats = compress_text("short", settings)
    error, error_stats = compress_text("Traceback (most recent call last):\nError: boom" * 20, settings)

    assert small == "short"
    assert small_stats.saved_bytes == 0
    assert "Traceback" in error
    assert error_stats.saved_bytes == 0


def test_rtk_preprocesses_tool_role_messages_only() -> None:
    settings = Settings(
        _env_file=None,
        KURUMI_PROXY_RTK_MIN_BYTES=100,
        KURUMI_PROXY_RTK_HEAD_LINES=1,
        KURUMI_PROXY_RTK_TAIL_LINES=1,
    )
    large = "\n".join(f"tool line {index}" for index in range(80))
    messages = [
        ChatMessage(role="user", content=large),
        ChatMessage(role="tool", content=large),
    ]

    processed, stats = preprocess_messages(messages, settings)

    assert processed[0].content == large
    assert isinstance(processed[1].content, str)
    assert len(processed[1].content) < len(large)
    assert stats.saved_bytes > 0
