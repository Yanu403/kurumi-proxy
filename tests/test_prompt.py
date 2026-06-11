from kurumi_proxy.models import ChatMessage
from kurumi_proxy.providers.codebuddy import build_prompt


def test_prompt_conversion_preserves_role_context() -> None:
    prompt = build_prompt(
        [
            ChatMessage(role="system", content="Be terse."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi."),
            ChatMessage(role="user", content="Summarize this."),
        ]
    )

    assert "System:\nBe terse." in prompt
    assert "Conversation:\nUser: Hello\nAssistant: Hi.\nUser: Summarize this." in prompt
    assert prompt.endswith("User:\nSummarize this.")


def test_prompt_notes_unsupported_content_blocks() -> None:
    prompt = build_prompt(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Describe this"},
                    {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
                ],
            )
        ]
    )

    assert "Describe this" in prompt
    assert "[Unsupported content block ignored: image_url]" in prompt
