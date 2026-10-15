"""
The HTTP API for the web app: builds the FastAPI app and includes every router in routes/.

Run with `make api` (uvicorn --app-dir app api:app --workers 1). One worker on purpose: the bundler's
nonces and the chat's rationing live in this process.
"""
import os
from contextlib import asynccontextmanager

import hvac.exceptions
from dotenv import load_dotenv
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import routes.added_tokens
import routes.allowlist
import routes.catalog
import routes.chat
import routes.contacts
import routes.deploy
import routes.history
import routes.owner
import routes.signin
import routes.telegram_link
import routes.wallet
from redact import install_log_redaction
from routes.deps import limiter
from smart_wallet_agent import (
    close_checkpointer,
    init_agent,
    open_checkpointer,
)

load_dotenv()


# uvicorn configures its log handlers before it imports this module, so they exist by now.
install_log_redaction()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Opens the agent's checkpointer and builds the agent on startup, closing it on shutdown.

    Exactly what telebot.py does around its bots. Without it `agent` stays None and every
    /api/chat request fails on the first attribute access.
    """
    await open_checkpointer()
    init_agent()
    try:
        yield
    finally:
        await close_checkpointer()


app = FastAPI(lifespan=lifespan)


app.state.limiter = limiter


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(hvac.exceptions.VaultError)
async def vault_unavailable(request: Request, exc: hvac.exceptions.VaultError):
    """
    Names Vault when it refuses a request, instead of a bare 500.

    Every session key is encrypted and decrypted through Vault, so deploys, grants and wallet reads
    all stop when it does. The everyday cause is the dev container restarting: it keeps everything
    in memory, so it comes back without the AppRole and the transit key, and the login in .env is
    refused. A bare 500 reached the web app only as "Something went wrong on our side", with
    nothing pointing at Vault. The exception itself goes to the log, not the client.
    """
    print(f"Vault error on {request.url.path}: {exc!r}")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "Key storage (Vault) refused the request, so no session key could be made or "
            "read. If the Vault container was restarted, run `make vault`, then restart the API."
        },
    )


# The refresh token travels as a cookie, so the browser must be allowed to send credentials --
# which means the allowed origins have to be listed explicitly (a wildcard is rejected by the
# browser alongside credentials, and would be wrong here anyway).
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(routes.signin.router)
app.include_router(routes.telegram_link.router)
app.include_router(routes.contacts.router)
app.include_router(routes.chat.router)
app.include_router(routes.history.router)
app.include_router(routes.catalog.router)
app.include_router(routes.added_tokens.router)
app.include_router(routes.deploy.router)
app.include_router(routes.wallet.router)
app.include_router(routes.owner.router)
app.include_router(routes.allowlist.router)
