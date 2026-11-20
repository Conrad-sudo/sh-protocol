"""
The MCP server AI apps call: /mcp, Streamable HTTP, stateless, for apps signed in through this
server's own OAuth (mcp_auth.py, routes/oauth.py).

The SDK (mcp 2.3.0) speaks the protocol -- both the initialize handshake (2024-11-05 to 2025-11-25)
and 2026-07-28's per-request envelope. Who is calling is decided before the SDK sees the request:
McpEndpoint checks the bearer token itself and hands the SDK the caller in the request's state, so
the SDK's own auth (AuthSettings, token_verifier) is not used. Its authorization-server half
declares what Claude needs to use a CIMD client id, and can't be told otherwise.

The tools: get_account, here, and the wallet's reads and quotes (mcp_tools.py), each with a
description that carries the rules the web chat's system prompt holds: these apps' models never see
that prompt. A quote hands the user a /c/<token> link; only their passkey sends it.
"""
import contextlib
import logging

import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

import mcp_auth
from constants import get_chain_display_name
from db import get_user_by_id, get_wallet_address, get_wallet_chains
from mcp_clients import APPS
from mcp_tools import CALLER_KEY, caller_of, count_call, wallet_tools

# Stateless, every request "terminates a session" -- an INFO line per call. Warnings still show.
logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)

# What every app is told about this server. Short, and the rules first: Claude's apps drop this
# text altogether (so every rule that matters is also in a tool's description or result), Claude
# Code keeps 2,048 characters, and ChatGPT reads the first 512 most closely.
INSTRUCTIONS = (
    "Mitfah is a self-custody smart wallet. This connection acts for one Mitfah account. It can read that account; it can never move funds on "
    "its own: every transaction is approved by the user with their passkey on Mitfah's own page. "
    "Never ask the user for a seed phrase, private key or passkey. "
    "The account has a smart wallet per network: get_account lists them, and the other tools take that network as `network`."
)

server = MCPServer(
    name="mitfah",
    title="Mitfah smart wallet",
    instructions=INSTRUCTIONS,
    version="1",
    tools=wallet_tools(),
)

# The running session manager: set while running() is open, which the API's lifespan holds.
_manager: StreamableHTTPSessionManager | None = None


@contextlib.asynccontextmanager
async def running():
    """
    Runs the MCP session manager for as long as the block is open (the API's lifespan).

    A session manager runs once in its life, so each opening builds a fresh one: the API starts
    once, but the tests start the app many times.

    No DNS-rebinding check (transport_security): the SDK switches one on by default that admits only
    localhost Host headers, which would turn away mitfah.com. It defends servers that answer
    without a credential; /mcp answers nothing without a valid token.
    """
    global _manager
    server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = server.session_manager
    async with manager.run():
        previous, _manager = _manager, manager
        try:
            yield
        finally:
            _manager = previous


def _bearer(scope: Scope) -> str:
    """The request's bearer token, or "" when there is none."""
    header = Headers(scope=scope).get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _unauthorized(scope: Scope, receive: Receive, send: Send, presented: bool):
    """
    401 with what MCP clients need to start signing in: where the protected-resource metadata is
    (RFC 9728), and -- when a token was sent -- that it is no good (RFC 6750).
    """
    challenge = f'Bearer resource_metadata="{mcp_auth.resource_metadata_url()}", scope="{mcp_auth.SCOPE}"'
    if presented:
        challenge += ', error="invalid_token", error_description="The access token is unknown, expired or revoked."'
    response = JSONResponse(
        {"error": "invalid_token" if presented else "unauthorized",
         "error_description": "Sign in to Mitfah to use this server."},
        status_code=401,
        headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"},
    )
    await response(scope, receive, send)


class McpEndpoint:
    """
    /mcp: checks the access token, then lets the SDK answer with the caller in the request's state.

    An ASGI app rather than a route function, so the SDK reads the body and writes the reply itself
    -- and so Starlette routes every method here (GET and DELETE answer 405 in stateless mode).
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        token = _bearer(scope)
        caller = await anyio.to_thread.run_sync(mcp_auth.verify_access, token) if token else None
        if caller is None:
            await _unauthorized(scope, receive, send, presented=bool(token))
            return
        if _manager is None:
            await JSONResponse({"error": "MCP is not running."}, status_code=503)(scope, receive, send)
            return
        state = {**scope.get("state", {}), CALLER_KEY: caller}
        await StreamableHTTPASGIApp(_manager)({**scope, "state": state}, receive, send)


# ── Tools ─────────────────────────────────────────────────────────────────────


# What the app's model reads about a tool is its description: written for that model, in full
# sentences, and passed explicitly (the SDK would pass the docstring as it is, indentation and all).
GET_ACCOUNT_DESCRIPTION = (
    "Which Mitfah account this app is connected to: the address that owns it, and its smart wallet's "
    "address on each network where it has one. Use it to check the connection works, or before talking "
    "about the user's wallets. Read-only."
)


@server.tool(
    name="get_account",
    title="Mitfah account",
    description=GET_ACCOUNT_DESCRIPTION,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def get_account(ctx: Context) -> dict:
    """The connected account and its wallets (GET_ACCOUNT_DESCRIPTION)."""
    caller = caller_of(ctx)
    count_call(caller)
    user = get_user_by_id(caller["user_id"])
    wallets = []
    for chain_id in get_wallet_chains(caller["user_id"]):
        wallets.append({
            "network": get_chain_display_name(chain_id),
            "chain_id": chain_id,
            "address": get_wallet_address(caller["user_id"], chain_id),
        })
    app = APPS.get(caller["app"])
    return {
        "owner_address": user["owner_addr"] if user else None,
        "wallets": wallets,
        "connected_app": app.name if app else caller["app"],
        "note": (
            "No smart wallet yet: the user creates one on Mitfah's website."
            if not wallets else
            "Funds sit in the smart wallets. The owner address only controls them."
        ),
    }
