"""
CodeBuddy ACP (Agent Client Protocol) provider.

This provider connects to a persistent CodeBuddy daemon via HTTP/SSE
and translates ACP events to OpenAI-compatible chat completions with
tool_calls, reasoning_content, and proper finish_reason support.
"""

from kurumi_proxy.providers.codebuddy_acp.daemon import AcpDaemon
from kurumi_proxy.providers.codebuddy_acp.session import AcpSession

__all__ = ["AcpDaemon", "AcpSession"]
