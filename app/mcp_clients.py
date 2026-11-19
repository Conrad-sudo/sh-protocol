"""
The AI apps that may connect to a Mitfah wallet over MCP: the known list.

Only apps on this list get as far as the Allow page. An app names itself with a client_id (for these
three, the web address of its client metadata document, CIMD) and says where to send the user back
(redirect_uri). Anyone can claim any client_id, so the client_id alone proves nothing. What makes
the list a real control is the redirect: the authorization code goes ONLY to an address the real
app owns, so a page pretending to be Claude never receives it. The redirects below are pinned to
those addresses, and the app's name on the Allow page comes from here, never from the request.

No CIMD document is fetched: everything needed is pinned below, so a document that changed (or a
server that answers slowly) can't change who is let in.

  - Claude (claude.ai, the desktop and phone apps): a CIMD client_id on claude.ai, sending the user
    back to Claude's OAuth callback. Anthropic asks for claude.com's callback to be allowed too.
  - Claude Code: https://claude.ai/oauth/claude-code-client-metadata, which lists
    http://localhost/callback and http://127.0.0.1/callback. A loopback redirect may use any port
    (RFC 8252 section 7.3): Claude Code listens on a free one.
  - ChatGPT: https://chatgpt.com/oauth/client.json, sending the user back to
    connector_platform_oauth_redirect, or to an older per-connector callback.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

CLAUDE_CODE_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
CHATGPT_CLIENT_ID = "https://chatgpt.com/oauth/client.json"

_CLAUDE_CALLBACKS = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
})
_CHATGPT_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
_CHATGPT_CONNECTOR_CALLBACK = re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


@dataclass(frozen=True)
class KnownApp:
    """An app on the known list, as the Allow page and Controls name it."""

    key: str     # what the database records: 'claude', 'claude-code' or 'chatgpt'
    name: str    # shown to the user
    site: str    # the app's own web address, shown under its name


CLAUDE = KnownApp("claude", "Claude", "claude.ai")
CLAUDE_CODE = KnownApp("claude-code", "Claude Code", "claude.ai")
CHATGPT = KnownApp("chatgpt", "ChatGPT", "chatgpt.com")

APPS = {app.key: app for app in (CLAUDE, CLAUDE_CODE, CHATGPT)}


def _is_claude_client(client_id: str) -> bool:
    """A CIMD client_id on claude.ai or claude.com: an https address with a path, nothing else."""
    parts = urlsplit(client_id)
    return (
        parts.scheme == "https"
        and parts.hostname in ("claude.ai", "claude.com")
        and parts.port is None
        and not parts.username
        and not parts.password
        and len(parts.path) > 1
        and not parts.fragment
    )


def _is_loopback_callback(redirect_uri: str) -> bool:
    """http://localhost:<port>/callback or http://127.0.0.1:<port>/callback, any port, nothing more."""
    try:
        parts = urlsplit(redirect_uri)
        port = parts.port   # raises on a port that isn't a number
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in _LOOPBACK_HOSTS
        and (port is None or 0 < port < 65536)
        and not parts.username
        and not parts.password
        and parts.path == "/callback"
        and not parts.query
        and not parts.fragment
    )


def find_app(client_id: str, redirect_uri: str) -> KnownApp | None:
    """
    The known app this client_id and redirect_uri belong to, or None when either doesn't match.

    Both must match the SAME app: Claude Code's client_id with claude.ai's callback is refused, as
    is any client_id with an address no listed app owns.

    @param client_id     The client_id the request names.
    @param redirect_uri  Where the request asks for the code to be sent. Compared exactly (but for a
                         loopback port), as OAuth 2.1 requires.
    """
    if client_id == CLAUDE_CODE_CLIENT_ID:
        return CLAUDE_CODE if _is_loopback_callback(redirect_uri) else None
    if client_id == CHATGPT_CLIENT_ID:
        ok = redirect_uri == _CHATGPT_CALLBACK or _CHATGPT_CONNECTOR_CALLBACK.fullmatch(redirect_uri) is not None
        return CHATGPT if ok else None
    if _is_claude_client(client_id):
        return CLAUDE if redirect_uri in _CLAUDE_CALLBACKS else None
    return None
