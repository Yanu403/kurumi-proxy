"""
CodeBuddy ACP (Agent Client Protocol) provider.

Connects to a CodeBuddy daemon via streamable-http and translates ACP
events to OpenAI-compatible chat completions.

Prerequisites:
- Operator must have run `codebuddy -p` (or interactive) once on this host
  so credentials are in `~/.codebuddy/local_storage/`.
- The daemon inherits those cached credentials at spawn time.
"""

from kurumi_proxy.providers.codebuddy_acp.daemon import AcpDaemon, AcpDaemonStartupError
from kurumi_proxy.providers.codebuddy_acp.client import AcpJsonRpcClient
from kurumi_proxy.providers.codebuddy_acp.session import (
    AcpSession,
    AcpAuthenticationRequiredError,
    AcpProtocolError,
    AcpUpstreamRefusalError,
)
from kurumi_proxy.providers.codebuddy_acp.translator import (
    translate_to_openai_stream,
    collect_openai_completion,
)

__all__ = [
    "AcpDaemon",
    "AcpDaemonStartupError",
    "AcpJsonRpcClient",
    "AcpSession",
    "AcpAuthenticationRequiredError",
    "AcpProtocolError",
    "AcpUpstreamRefusalError",
    "translate_to_openai_stream",
    "collect_openai_completion",
]
