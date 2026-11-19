"""
Connecting AI apps (Claude, Claude Code, ChatGPT) to a Mitfah account over MCP: this server's own
OAuth 2.1 endpoints, the API behind the Allow page (/connect), and the account's connected apps.

The rules live in mcp_auth.py and the known list in mcp_clients.py. The flow:

  1. The app finds us: /mcp answers 401 naming /.well-known/oauth-protected-resource/mcp, which
     names this server as the authorization server; its metadata says CIMD client ids are
     welcome and no client secret is used.
  2. The app opens /api/oauth/authorize in the user's browser. An app not on the known list, or
     asking for its code at an address that app doesn't own, is shown an error on OUR page and is
     never redirected to. Otherwise the request is filed, the browser gets a binding cookie, and
     goes on to /connect?request=<id> -- which signs the user in first if needed.
  3. The Allow page shows the app (named from the known list) and the one permission. Allow or
     Deny answers with where to send the browser: the app's redirect, with the code or the refusal.
  4. The app swaps the code at /api/oauth/token (PKCE) and calls /mcp with the access token.

What the binding cookie does: only the browser that opened a request may answer it, so a
/connect?request= link copied out of one browser is refused in any other. What it can't do: show
who STARTED the flow. Someone can send a signed-in victim the app's own /api/oauth/authorize link,
carrying the state of THEIR copy of the app; the victim's browser then opens a fresh request, and
Allow sends the code to the real app's callback in the victim's browser. Only the app's own check
that the state belongs to that browser keeps the victim's account from joining the sender's app
account. Hence the Allow page's warning to allow only a connection the user just started, and a
connection never sends (THREAT_MODEL §4.12).
"""
import re
import secrets
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

import mcp_auth
from auth import get_current_user
from db import get_mcp_connections, purge_expired_mcp_tokens, revoke_mcp_connection
from mcp_clients import APPS, find_app
from routes.deps import limiter
from routes.signin import COOKIE_SECURE

router = APIRouter()

# The cookie that ties a request to connect to the browser that opened it. Scoped to /api/oauth, so
# it goes nowhere else; httpOnly, so no script can read it. One per browser, reused by every request
# it opens, so connecting two apps at once works.
BINDING_COOKIE = "oauth_binding"
BINDING_COOKIE_PATH = "/api/oauth"
BINDING_COOKIE_MAX_AGE = 60 * 60
_BINDING = re.compile(r"[A-Za-z0-9_-]{43}")

# The token endpoint's form is small; anything bigger is not a token request.
MAX_FORM_BYTES = 16 * 1024

# OAuth answers must not be cached (RFC 6749 section 5.1).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _app_shown(key: str) -> dict:
    """An app as the web app shows it: its name and web address from the known list."""
    app = APPS.get(key)
    return {"key": key, "name": app.name if app else key, "site": app.site if app else ""}


# ── Discovery ─────────────────────────────────────────────────────────────────


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata():
    """
    RFC 8414 metadata. Declares what Claude and ChatGPT look for before using a CIMD client id:
    client_id_metadata_document_supported, and "none" among the token endpoint's auth methods
    (public clients: PKCE stands in for a secret). No registration endpoint: apps are on the known
    list or not at all.
    """
    iss = mcp_auth.issuer()
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint": f"{iss}/api/oauth/authorize",
        "token_endpoint": f"{iss}/api/oauth/token",
        "revocation_endpoint": f"{iss}/api/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [mcp_auth.SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    })


def _protected_resource_metadata() -> JSONResponse:
    """RFC 9728 metadata for /mcp: which server signs apps in for it, and the one permission."""
    return JSONResponse({
        "resource": mcp_auth.resource(),
        "authorization_servers": [mcp_auth.issuer()],
        "scopes_supported": [mcp_auth.SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Mitfah smart wallet",
    })


@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata():
    """Where /mcp's 401 points (RFC 9728 path insertion)."""
    return _protected_resource_metadata()


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata_root():
    """The same, at the root, for apps that look there first."""
    return _protected_resource_metadata()


# ── Authorization ─────────────────────────────────────────────────────────────


@router.get("/api/oauth/authorize")
@limiter.limit("60/minute")
def authorize(request: Request, binding: str | None = Cookie(default=None, alias=BINDING_COOKIE)):
    """
    An app asks to connect: checks the request, files it and sends the browser to the Allow page.

    Errors before the app is known go to our own /connect page, never to the app's redirect: an
    address nobody vouched for must not receive anything (an open redirect). Once the client and
    its redirect match the known list, the remaining errors go back to the app, as OAuth says.
    """
    params = request.query_params
    if any(len(params.getlist(key)) > 1 for key in params.keys()):
        return RedirectResponse("/connect?error=invalid_request", status_code=status.HTTP_302_FOUND)
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    app = find_app(client_id, redirect_uri) if client_id and redirect_uri else None
    if app is None:
        return RedirectResponse("/connect?error=unknown_app", status_code=status.HTTP_302_FOUND)

    state = params.get("state")

    def back_to_app(error: str, description: str, echo_state: bool = True) -> RedirectResponse:
        return RedirectResponse(
            mcp_auth.with_params(redirect_uri, error=error, error_description=description,
                                 state=state if echo_state else None, iss=mcp_auth.issuer()),
            status_code=status.HTTP_302_FOUND,
        )

    if state is not None and len(state) > mcp_auth.MAX_STATE_LEN:
        return back_to_app("invalid_request", "state is too long.", echo_state=False)
    if params.get("response_type") != "code":
        return back_to_app("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge", "")
    if params.get("code_challenge_method") != "S256" or not mcp_auth.is_s256_challenge(challenge):
        return back_to_app("invalid_request", "PKCE is required, with code_challenge_method=S256.")
    resource = params.get("resource")
    if resource is not None and not mcp_auth.is_our_resource(resource):
        return back_to_app("invalid_target", f"This server only issues tokens for {mcp_auth.resource()}.")
    # Whatever scope is asked for, the one permission is what is granted, and the token response
    # says so (RFC 6749 section 3.3).

    if not binding or not _BINDING.fullmatch(binding):
        binding = secrets.token_urlsafe(32)
    request_id = mcp_auth.open_request(app.key, client_id, redirect_uri, state, challenge, binding)
    response = RedirectResponse(f"/connect?{urlencode({'request': request_id})}", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        BINDING_COOKIE,
        binding,
        max_age=BINDING_COOKIE_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path=BINDING_COOKIE_PATH,
    )
    return response


def _answerable(request_id: str, binding: str | None) -> dict:
    """mcp_auth.answerable_request, with its problems as HTTP errors."""
    try:
        return mcp_auth.answerable_request(request_id, binding)
    except mcp_auth.RequestProblem as e:
        raise HTTPException(e.status, e.message)


@router.get("/api/oauth/requests/{request_id}")
def get_request(
    request_id: str = Path(max_length=64),
    user_id: int = Depends(get_current_user),
    binding: str | None = Cookie(default=None, alias=BINDING_COOKIE),
):
    """
    What the Allow page shows: which app asks (from the known list), and until when it may answer.

    @raises HTTPException 404 / 403 / 410 as mcp_auth.answerable_request.
    """
    found = _answerable(request_id, binding)
    return {"id": found["id"], "app": _app_shown(found["app"]), "expires_at": found["expires_at"]}


@router.post("/api/oauth/requests/{request_id}/approve")
def approve_request(
    request_id: str = Path(max_length=64),
    user_id: int = Depends(get_current_user),
    binding: str | None = Cookie(default=None, alias=BINDING_COOKIE),
):
    """
    Allow: the signed-in account connects the app.

    @return  {"redirect_to": the app's redirect, with the code, state and iss}.
    """
    try:
        return {"redirect_to": mcp_auth.approve(request_id, binding, user_id)}
    except mcp_auth.RequestProblem as e:
        raise HTTPException(e.status, e.message)


@router.post("/api/oauth/requests/{request_id}/deny")
def deny_request(
    request_id: str = Path(max_length=64),
    user_id: int = Depends(get_current_user),
    binding: str | None = Cookie(default=None, alias=BINDING_COOKIE),
):
    """
    Deny: the app is told the user refused.

    @return  {"redirect_to": the app's redirect, with error=access_denied, state and iss}.
    """
    try:
        return {"redirect_to": mcp_auth.deny(request_id, binding)}
    except mcp_auth.RequestProblem as e:
        raise HTTPException(e.status, e.message)


# ── Tokens ────────────────────────────────────────────────────────────────────


async def _form(request: Request) -> dict[str, str] | None:
    """
    The token endpoint's application/x-www-form-urlencoded body, or None when it isn't one: another
    content type, too big, not UTF-8, or a parameter sent twice (RFC 6749 section 3.2).
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return None
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return None
    form = dict(pairs)
    return form if len(form) == len(pairs) else None


def _oauth_error(error: mcp_auth.OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=error.status,
        headers=NO_STORE,
    )


@router.post("/api/oauth/token")
def token(form: dict[str, str] | None = Depends(_form)):
    """
    Swaps an authorization code, or a refresh token, for tokens (see mcp_auth). Public clients only:
    no client secret is asked for. ChatGPT may authenticate with a signed JWT (private_key_jwt); it
    is not checked and not needed, since PKCE and the pinned redirect already bind the code to the
    real app.

    Not rate-limited by IP: the apps call from their own servers, so one address speaks for many
    users. Every code and token is 256 bits of randomness, so there is nothing to guess.
    """
    if form is None:
        return _oauth_error(mcp_auth.OAuthError("invalid_request", "Send the parameters as application/x-www-form-urlencoded, once each."))
    grant_type = form.get("grant_type")
    try:
        if grant_type == "authorization_code":
            purge_expired_mcp_tokens()
            body = mcp_auth.exchange_code(
                form.get("code"), form.get("client_id"), form.get("redirect_uri"),
                form.get("code_verifier"), form.get("resource"),
            )
        elif grant_type == "refresh_token":
            body = mcp_auth.refresh(form.get("refresh_token"), form.get("client_id"), form.get("resource"))
        else:
            raise mcp_auth.OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
    except mcp_auth.OAuthError as e:
        return _oauth_error(e)
    return JSONResponse(body, headers=NO_STORE)


@router.post("/api/oauth/revoke")
def revoke(form: dict[str, str] | None = Depends(_form)):
    """
    RFC 7009 revocation: a refresh token ends its connection, an access token only itself. Answers
    200 whatever the token, so it reveals nothing about which tokens exist.
    """
    if form is None:
        return _oauth_error(mcp_auth.OAuthError("invalid_request", "Send the token as application/x-www-form-urlencoded."))
    mcp_auth.revoke(form.get("token"))
    return Response(status_code=status.HTTP_200_OK, headers=NO_STORE)


# ── Connected apps (Controls) ─────────────────────────────────────────────────


@router.get("/api/connections")
def list_connections(user_id: int = Depends(get_current_user)):
    """
    The AI apps connected to this account, newest first.

    @return  {"connections": [{"id", "app": {"key", "name", "site"}, "created_at", "last_used_at"}]}.
    """
    return {
        "connections": [
            {
                "id": row["id"],
                "app": _app_shown(row["app"]),
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
            }
            for row in get_mcp_connections(user_id)
        ]
    }


@router.delete("/api/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(connection_id: int, user_id: int = Depends(get_current_user)):
    """
    Disconnects an app: its tokens stop working on the very next call.

    @raises HTTPException 404 if the account has no such live connection.
    """
    if not revoke_mcp_connection(connection_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connected app.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
